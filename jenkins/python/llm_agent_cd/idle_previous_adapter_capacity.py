"""Release one duplicate E2 adapter for the idle workflow previous release.

The workflow Service keeps its independently scheduled N2 adapter.  This
action is deliberately narrow: it cannot run during an experiment, cannot
change state or routing, and restores the adapter if the exact Qwen2.5
candidate preflight does not pass after the reversible capacity exchange.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import time

from apps.agentic.llm_ab_router.trigger import candidate_from_config
from .driver import Driver, command
from .provision import kube
from .release import digest
from .release_guard import check
from .state import StateStore


REVIEW = "reviewed-idle-previous-single-adapter-v1"
REVIEW_CPU = "reviewed-prompt-previous-cpu-adapter-v2"
CATALOG = "configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m-tools-v3.json"
FIXTURES = "configs/llm-ab/workflow-cases-v11.json"


def validate_state(state: dict) -> tuple[str, str]:
    champion_id = (state.get("champion") or {}).get("release_id", "")
    release_id = (state.get("previous") or {}).get("release_id", "")
    if (
        state.get("phase") != "IDLE"
        or not state.get("activated")
        or state.get("experiment_id")
        or not re.fullmatch(r"[0-9a-f]{64}", champion_id)
        or not re.fullmatch(r"[0-9a-f]{64}", release_id)
        or release_id == champion_id
    ):
        raise ValueError("exact activated IDLE champion/previous state required")
    return champion_id, release_id


def deployment(name: str, pool: str, release_id: str, replicas: set[int]) -> dict:
    obj = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
    env = [
        entry
        for container in obj["spec"]["template"]["spec"]["containers"]
        for entry in container.get("env", [])
        if entry["name"] == "RELEASE_ID"
    ]
    actual = obj["spec"].get("replicas")
    if (
        obj["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
        or actual not in replicas
        or (actual == 1 and obj.get("status", {}).get("readyReplicas") != 1)
        or obj["spec"]["template"]["spec"].get("nodeSelector")
        != {"recsys.ai/pool": pool}
        or env != [{"name": "RELEASE_ID", "value": release_id}]
    ):
        raise ValueError("previous adapter identity/readiness drift: " + name)
    return obj


def ready_endpoints(service: str) -> int:
    slices = json.loads(
        kube(
            "kagent",
            "get",
            "endpointslice",
            "-l",
            "kubernetes.io/service-name=" + service,
            "-o",
            "json",
        )
    )["items"]
    return sum(
        endpoint.get("conditions", {}).get("ready") is True
        for item in slices
        for endpoint in item.get("endpoints", [])
    )


def exact_candidate(state: dict, coordinator_only: bool = False) -> dict:
    llm = json.loads(Path(CATALOG).read_text())
    config = {
        "schema_version": 1,
        "scope": "workflow",
        "baseline_workflow_release_id": state["champion"]["release_id"],
        "global_generation": deepcopy(state["champion"]["global_generation"]),
        "llm_release_ref": digest(llm),
        "experiment_type": "llm_only",
        "policy_ref": "workflow-live-test",
    }
    if coordinator_only:
        config["target_role"] = "coordinator"
    return candidate_from_config(state["champion"], config, llm)


def restore_target(name: str, pool: str, release_id: str) -> None:
    obj = deployment(name, pool, release_id, {0, 1})
    if obj["spec"].get("replicas") == 0:
        kube(
            "kagent",
            "patch",
            "deployment",
            name,
            "--type=json",
            "-p",
            json.dumps(
                [
                    {
                        "op": "test",
                        "path": "/metadata/resourceVersion",
                        "value": obj["metadata"]["resourceVersion"],
                    },
                    {"op": "test", "path": "/spec/replicas", "value": 0},
                    {"op": "replace", "path": "/spec/replicas", "value": 1},
                ]
            ),
        )
    kube("kagent", "rollout", "status", "deployment/" + name, "--timeout=180s")
    deployment(name, pool, release_id, {1})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    review = parser.parse_args().image
    if review not in {REVIEW, REVIEW_CPU}:
        raise ValueError("unreviewed idle previous capacity action")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")

    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = json.loads(
        kube("kagent", "get", "deployment", "recsys-workflow-router", "-o", "json")
    )
    os.environ.update(
        AB_SCOPE="workflow",
        AB_SECRET_NAME="recsys-workflow-runtime",
        AB_ALLOW_LIVE_TEST="true",
        AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0]["image"],
    )
    check("workflow")
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    champion_id, release_id = validate_state(state)
    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("workflow route is not the verified champion route")

    base = "rec-ab-" + release_id[:20]
    cpu_target = review == REVIEW_CPU
    target_name = base + "-cpu" if cpu_target else base
    target_pool = "cpu-services" if cpu_target else "ml-system"
    peer_name = base if cpu_target else base + "-cpu"
    peer_pool = "ml-system" if cpu_target else "cpu-services"
    target = deployment(target_name, target_pool, release_id, {0, 1})
    peer = deployment(peer_name, peer_pool, release_id, {1})
    if ready_endpoints(base) < 1:
        raise ValueError("previous Service has no Ready endpoint")

    with driver.db.connect() as connection:
        counts = {
            "sessions": connection.execute(
                "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s",
                (release_id,),
            ).fetchone()["n"],
            "unfinished": connection.execute(
                "SELECT count(*) n FROM recsys_ab.invocations "
                "WHERE release_id=%s AND finished_at IS NULL",
                (release_id,),
            ).fetchone()["n"],
            "active_dispatches": connection.execute(
                "SELECT count(*) n FROM recsys_ab.trigger_requests "
                "WHERE status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED','RUNNING')"
            ).fetchone()["n"],
        }
    if counts["unfinished"] or counts["active_dispatches"]:
        raise ValueError("active workflow or dispatch blocks adapter placement change")

    candidate = exact_candidate(state, coordinator_only=cpu_target)
    key = (
        "workflow/capacity-windows/idle-previous-"
        + ("cpu" if cpu_target else "ml-system")
        + "-adapter-"
        + release_id
        + "-"
        + os.environ["BUILD_NUMBER"]
        + ".json"
    )
    snapshot = {
        "stage": "idle_previous_single_adapter_intent",
        "build_url": os.environ["BUILD_URL"],
        "state_etag": etag,
        "release_id": release_id,
        "champion_release_id": champion_id,
        "candidate_release_id": candidate["release_id"],
        "target": target,
        "retained_peer": peer,
        "counts": counts,
        "route_revision": state["route_revision"],
    }
    store.client.put_object(
        Bucket=store.bucket,
        Key=key,
        Body=json.dumps(snapshot, sort_keys=True).encode(),
        ContentType="application/json",
        IfNoneMatch="*",
    )

    changed = target["spec"].get("replicas") == 1
    try:
        if changed:
            kube(
                "kagent",
                "patch",
                "deployment",
                target_name,
                "--type=json",
                "-p",
                json.dumps(
                    [
                        {
                            "op": "test",
                            "path": "/metadata/resourceVersion",
                            "value": target["metadata"]["resourceVersion"],
                        },
                        {"op": "test", "path": "/spec/replicas", "value": 1},
                        {"op": "replace", "path": "/spec/replicas", "value": 0},
                    ]
                ),
            )
            selector = target["spec"]["selector"]["matchLabels"]
            deadline = time.monotonic() + 180
            while True:
                pods = json.loads(
                    command("kubectl", "get", "pods", "-n", "kagent", "-o", "json")
                )["items"]
                admitted = [
                    pod
                    for pod in pods
                    if pod.get("status", {}).get("phase")
                    not in {"Succeeded", "Failed"}
                    and all(
                        pod["metadata"].get("labels", {}).get(key) == value
                        for key, value in selector.items()
                    )
                ]
                if not admitted:
                    break
                if time.monotonic() > deadline:
                    raise ValueError(
                        "HOLD graceful previous adapter termination; no force delete"
                    )
                time.sleep(3)

        target_after = deployment(target_name, target_pool, release_id, {0})
        peer_after = deployment(peer_name, peer_pool, release_id, {1})
        if ready_endpoints(base) < 1:
            raise ValueError("retained N2 previous adapter has no Ready Service endpoint")

        # This is the exact manifest/fixture/capacity check used by Workflow-CD,
        # now evaluated against admitted pods after the reversible exchange.
        driver.preflight(
            state["champion"],
            candidate,
            json.loads(Path(FIXTURES).read_text()),
        )
        after, after_etag = store.read()
        if (
            after != state
            or after_etag != etag
            or not driver.verify_route(after, 0, after["route_revision"])
        ):
            raise ValueError("state/route preservation failure")
    except Exception:
        if changed:
            restore_target(target_name, target_pool, release_id)
        raise

    report = {
        "stage": "idle_previous_single_adapter",
        "snapshot_key": key,
        "release_id": release_id,
        "champion_release_id": champion_id,
        "candidate_release_id": candidate["release_id"],
        "candidate_change_scope": candidate.get("change_scope", "workflow"),
        "target_scaled_to_zero": target_after["metadata"]["name"],
        "retained_ready_peer": peer_after["metadata"]["name"],
        "sessions_retained": counts["sessions"],
        "state_unchanged": True,
        "route_unchanged": True,
        "exact_candidate_capacity_verified": True,
        "inference_requests": 0,
    }
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
