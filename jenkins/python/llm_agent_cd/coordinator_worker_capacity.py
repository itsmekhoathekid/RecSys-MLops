"""Raise only the Coordinator WorkerPool to two replicas under release locks.

The change is a bounded production capacity reservation for Coordinator-only
A/B.  Context and Recommendation remain at one replica.  The exact future
candidate is included in the capacity preflight, so success proves both the
second worker and the unrouted candidate adapter fit with the configured node
headroom.  No inference, route, champion or dispatch state is changed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path

from apps.agentic.llm_ab_router.trigger import candidate_from_config
from .driver import Driver
from .provision import secret
from .release import digest
from .release_guard import check
from .state import StateStore
from .terminal_candidate_capacity_window import (
    _get,
    _patch_scaled_object,
    _restore,
    _scaled_pool,
    _wait_worker_pool,
)


WINDOW = "coordinator-workers-two-v1"
BASELINE = "2458bfb95a8e3310adeea29ab39fe2f5ebf963d4f48060356fdc8c8fb84385c6"
POOL = "recsys-coordinator-sandbox-pool"
CATALOG = "configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m-tools-v3.json"
FIXTURES = "configs/llm-ab/workflow-cases-v11.json"


def coordinator_candidate(baseline: dict, namespace: str = "kagent") -> dict:
    llm = json.loads(Path(CATALOG).read_text())
    config = {
        "schema_version": 1,
        "scope": "workflow",
        "baseline_workflow_release_id": baseline["release_id"],
        "global_generation": deepcopy(baseline["global_generation"]),
        "llm_release_ref": digest(llm),
        "experiment_type": "llm_only",
        "policy_ref": "workflow-live-test",
        "target_role": "coordinator",
    }
    return candidate_from_config(baseline, config, llm, namespace)


def _active_work(driver: Driver) -> tuple[int, int]:
    with driver.db.connect() as connection:
        unfinished = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations WHERE finished_at IS NULL"
        ).fetchone()["n"]
        dispatching = connection.execute(
            "SELECT count(*) n FROM recsys_ab.trigger_requests "
            "WHERE status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED','RUNNING')"
        ).fetchone()["n"]
    return unfinished, dispatching


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    if parser.parse_args().image != WINDOW:
        raise ValueError("unreviewed Coordinator capacity action")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")

    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = _get("kagent", "deployment", "recsys-workflow-router")
    os.environ.update(
        AB_SCOPE="workflow",
        AB_SECRET_NAME="recsys-workflow-runtime",
        AB_ALLOW_LIVE_TEST="true",
        AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0]["image"],
    )
    check("workflow")
    trigger = secret("kagent", "recsys-workflow-trigger")
    if trigger.get("AB_DISPATCH_ENABLED") != "false":
        raise ValueError("dispatch must remain disabled during Coordinator capacity change")

    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    if (
        state.get("phase") != "IDLE"
        or not state.get("activated")
        or state.get("experiment_id")
        or state.get("champion", {}).get("release_id") != BASELINE
        or state.get("baseline", {}).get("release_id") != BASELINE
        or state.get("pending", {}).get("release_id") != BASELINE
    ):
        raise ValueError("exact activated IDLE baseline required")

    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("baseline route is not verified")
    if any(_active_work(driver)):
        raise ValueError("active invocation or dispatch blocks capacity change")

    pool = _scaled_pool(POOL, {(1, 1), (2, 2)})
    candidate = coordinator_candidate(state["champion"])
    from .workflow import members
    before, after = members(state["champion"]), members(candidate)
    if any(
        before[role]["llm_version_id"] != after[role]["llm_version_id"]
        for role in ("context", "recommendation")
    ):
        raise ValueError("Coordinator capacity candidate changed a specialist LLM")

    journal = (
        "workflow/capacity-windows/coordinator-workers-two-"
        + BASELINE
        + "-"
        + os.environ["BUILD_NUMBER"]
        + ".json"
    )
    snapshot = {
        "stage": "coordinator_worker_capacity_intent",
        "window": WINDOW,
        "build_url": os.environ["BUILD_URL"],
        "state_etag": etag,
        "route_revision": state["route_revision"],
        "baseline_release_id": BASELINE,
        "candidate_release_id": candidate["release_id"],
        "change_scope": candidate["change_scope"],
        "worker_pool": pool,
    }
    store.client.put_object(
        Bucket=store.bucket,
        Key=journal,
        Body=json.dumps(snapshot, sort_keys=True).encode(),
        ContentType="application/json",
        IfNoneMatch="*",
    )

    actions: list[dict] = []
    try:
        scaled = pool["scaled_object"]
        worker = pool["worker_pool"]
        if (scaled["spec"].get("minReplicaCount"),
                scaled["spec"].get("maxReplicaCount")) == (1, 1):
            action = {
                "type": "scaled_pool",
                "name": POOL,
                "original": [1, 1],
                "desired": [2, 2],
                "uid": scaled["metadata"]["uid"],
                "worker_uid": worker["metadata"]["uid"],
            }
            _patch_scaled_object(POOL, (1, 1), (2, 2), action["uid"])
            actions.append(action)
            _wait_worker_pool(POOL, 2, action["worker_uid"])

        # This includes the second worker, candidate backend/adapters, Jobs,
        # pod overhead and the mandatory 200m/128Mi per-node headroom.
        driver.preflight(
            state["champion"],
            candidate,
            json.loads(Path(FIXTURES).read_text()),
        )
        after_state, after_etag = store.read()
        if (
            after_state != state
            or after_etag != etag
            or any(_active_work(driver))
            or not driver.verify_route(after_state, 0, after_state["route_revision"])
        ):
            raise ValueError("state, work queue, or route changed during capacity action")
    except Exception as original:
        try:
            _restore(actions)
        except Exception as rollback:
            raise RuntimeError(str(rollback)) from original
        raise

    report = {
        "stage": "coordinator_worker_capacity",
        "window": WINDOW,
        "journal_key": journal,
        "baseline_release_id": BASELINE,
        "candidate_release_id": candidate["release_id"],
        "change_scope": "coordinator",
        "coordinator_workers": 2,
        "context_workers": 1,
        "recommendation_workers": 1,
        "exact_candidate_capacity_preflight": "PASS",
        "dispatch_enabled": False,
        "state_unchanged": True,
        "route_unchanged": True,
        "inference_requests": 0,
    }
    target = Path(".llm-agent-cd/evaluation-preparation.json")
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
