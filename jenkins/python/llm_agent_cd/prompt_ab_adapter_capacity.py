"""Exchange one duplicate adapter per node for prompt-frozen Coordinator A/B.

The active champion remains untouched.  The previous release keeps its
CPU-services endpoint, while one exact historical release keeps its ml-system
endpoint.  Both Services therefore remain usable while 25m is released on
each node.  Any failed exact-candidate preflight restores every adapter changed
by this action.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

from .driver import Driver, command
from .idle_previous_adapter_capacity import (
    deployment,
    exact_candidate,
    ready_endpoints,
    restore_target,
    validate_state,
)
from .provision import kube, secret
from .release_guard import check
from .state import StateStore


REVIEW = "reviewed-prompt-ab-cross-node-adapter-exchange-v1"
REVIEW_RESTORE = "reviewed-prompt-ab-cross-node-adapter-restore-v1"
REVIEW_COORDINATOR = "reviewed-coordinator-ab-previous-ml-sessionless-history-cpu-v2"
FIXTURES = "configs/llm-ab/workflow-cases-v11.json"
HISTORICAL_RELEASE = "ea6a8e9318cb41073745254893fb64d4c5cdec26850959a7b8498ba7ce7ab2f2"
SESSIONLESS_HISTORY_RELEASE = "2458bfb95a8e3310adeea29ab39fe2f5ebf963d4f48060356fdc8c8fb84385c6"


def _coordinator_window(store, state, etag, driver, champion_id, previous_id) -> None:
    """Free 25m on each node without removing a serving release.

    The previous release keeps its CPU peer for manual rollback.  The older
    release has no state pointer, session or unfinished invocation, so its
    final CPU adapter may be stopped while immutable evidence is retained.
    """
    previous_base = "rec-ab-" + previous_id[:20]
    history_base = "rec-ab-" + SESSIONLESS_HISTORY_RELEASE[:20]
    targets = [
        deployment(previous_base, "ml-system", previous_id, {0, 1}),
        deployment(history_base + "-cpu", "cpu-services",
                   SESSIONLESS_HISTORY_RELEASE, {0, 1}),
    ]
    retained = deployment(previous_base + "-cpu", "cpu-services", previous_id, {1})
    if ready_endpoints(previous_base) < 1:
        raise ValueError("previous release has no Ready endpoint")
    pointers = {(state.get(key) or {}).get("release_id")
                for key in ("champion", "previous", "baseline", "pending")}
    route = json.loads(kube(
        "kagent", "get", "virtualservice", "recsys-workflow-ab", "-o", "json"))
    route_reference_retained = SESSIONLESS_HISTORY_RELEASE in json.dumps(route["spec"])
    if SESSIONLESS_HISTORY_RELEASE in pointers:
        raise ValueError("sessionless historical release became a serving pointer")
    with driver.db.connect() as connection:
        history_sessions = connection.execute(
            "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s",
            (SESSIONLESS_HISTORY_RELEASE,),
        ).fetchone()["n"]
        history_unfinished = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations "
            "WHERE release_id=%s AND finished_at IS NULL",
            (SESSIONLESS_HISTORY_RELEASE,),
        ).fetchone()["n"]
        previous_unfinished = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations "
            "WHERE release_id=%s AND finished_at IS NULL",
            (previous_id,),
        ).fetchone()["n"]
    if history_sessions or history_unfinished or previous_unfinished:
        raise ValueError("session or unfinished invocation blocks capacity window")

    candidate = exact_candidate(state, coordinator_only=True)
    journal = (
        "workflow/capacity-windows/coordinator-ab-adapters-"
        + champion_id + "-" + os.environ["BUILD_NUMBER"] + ".json"
    )
    store.client.put_object(
        Bucket=store.bucket,
        Key=journal,
        Body=json.dumps({
            "stage": "coordinator_ab_adapter_capacity_intent",
            "build_url": os.environ["BUILD_URL"],
            "state_etag": etag,
            "route_revision": state["route_revision"],
            "champion_release_id": champion_id,
            "previous_release_id": previous_id,
            "sessionless_history_release_id": SESSIONLESS_HISTORY_RELEASE,
            "candidate_release_id": candidate["release_id"],
            "targets": targets,
            "retained_previous_peer": retained,
            "history_sessions": history_sessions,
            "historical_route_entry_retained": route_reference_retained,
        }, sort_keys=True).encode(),
        ContentType="application/json",
        IfNoneMatch="*",
    )
    changed: list[tuple[str, str, str]] = []
    try:
        for obj, pool, release_id in (
            (targets[0], "ml-system", previous_id),
            (targets[1], "cpu-services", SESSIONLESS_HISTORY_RELEASE),
        ):
            if obj["spec"].get("replicas") == 1:
                _scale_zero(obj)
                changed.append((obj["metadata"]["name"], pool, release_id))
        deployment(previous_base, "ml-system", previous_id, {0})
        deployment(history_base + "-cpu", "cpu-services",
                   SESSIONLESS_HISTORY_RELEASE, {0})
        deployment(previous_base + "-cpu", "cpu-services", previous_id, {1})
        if ready_endpoints(previous_base) < 1:
            raise ValueError("retained previous endpoint is not Ready")
        driver.preflight(
            state["champion"], candidate,
            json.loads(Path(FIXTURES).read_text()),
        )
        after, after_etag = store.read()
        if (
            after != state
            or after_etag != etag
            or not driver.verify_route(after, 0, after["route_revision"])
        ):
            raise ValueError("state or route changed during capacity window")
    except Exception:
        for name, pool, release_id in reversed(changed):
            restore_target(name, pool, release_id)
        raise
    report = {
        "stage": "coordinator_ab_adapter_capacity",
        "journal_key": journal,
        "champion_release_id": champion_id,
        "previous_release_id": previous_id,
        "sessionless_history_release_id": SESSIONLESS_HISTORY_RELEASE,
        "candidate_release_id": candidate["release_id"],
        "scaled_to_zero": [obj["metadata"]["name"] for obj in targets],
        "retained_ready": [retained["metadata"]["name"]],
        "released_cpu_each_node": "25m",
        "historical_route_entry_retained": route_reference_retained,
        "exact_coordinator_candidate_capacity_verified": True,
        "state_unchanged": True,
        "route_unchanged": True,
        "inference_requests": 0,
    }
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(
        json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


def _scale_zero(obj: dict) -> None:
    kube(
        "kagent", "patch", "deployment", obj["metadata"]["name"],
        "--type=json", "-p", json.dumps([
            {"op": "test", "path": "/metadata/resourceVersion",
             "value": obj["metadata"]["resourceVersion"]},
            {"op": "test", "path": "/spec/replicas", "value": 1},
            {"op": "replace", "path": "/spec/replicas", "value": 0},
        ]),
    )
    selector = obj["spec"]["selector"]["matchLabels"]
    deadline = time.monotonic() + 180
    while True:
        pods = json.loads(command(
            "kubectl", "-n", "kagent", "get", "pods", "-o", "json"))["items"]
        admitted = [pod for pod in pods
            if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            and all(pod["metadata"].get("labels", {}).get(key) == value
                    for key, value in selector.items())]
        if not admitted:
            return
        if time.monotonic() > deadline:
            raise ValueError("HOLD graceful duplicate-adapter termination; no force delete")
        time.sleep(3)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    review = parser.parse_args().image
    if review not in {REVIEW, REVIEW_RESTORE, REVIEW_COORDINATOR}:
        raise ValueError("unreviewed prompt A/B adapter exchange")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")

    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = json.loads(kube(
        "kagent", "get", "deployment", "recsys-workflow-router", "-o", "json"))
    os.environ.update(
        AB_SCOPE="workflow",
        AB_SECRET_NAME="recsys-workflow-runtime",
        AB_ALLOW_LIVE_TEST="true",
        AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0]["image"],
    )
    check("workflow")
    trigger = secret("kagent", "recsys-workflow-trigger")
    if not trigger or trigger.get("AB_DISPATCH_ENABLED") != "false":
        raise ValueError("dispatch must remain disabled during capacity exchange")

    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    champion_id, previous_id = validate_state(state)
    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("workflow route is not the verified champion route")
    with driver.db.connect() as connection:
        unfinished = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations WHERE finished_at IS NULL"
        ).fetchone()["n"]
        dispatches = connection.execute(
            "SELECT count(*) n FROM recsys_ab.trigger_requests "
            "WHERE status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED','RUNNING')"
        ).fetchone()["n"]
    if unfinished or dispatches:
        raise ValueError("active invocation or dispatch blocks capacity exchange")

    if review == REVIEW_COORDINATOR:
        _coordinator_window(store, state, etag, driver, champion_id, previous_id)
        return

    pointers = {(state.get(key) or {}).get("release_id")
                for key in ("champion", "previous", "baseline", "pending")}
    if HISTORICAL_RELEASE in pointers:
        raise ValueError("reviewed historical release became a serving pointer")
    previous_base = "rec-ab-" + previous_id[:20]
    historical_base = "rec-ab-" + HISTORICAL_RELEASE[:20]
    restore = review == REVIEW_RESTORE
    expected_target_replicas = {0} if restore else {0, 1}
    targets = [
        deployment(previous_base, "ml-system", previous_id, expected_target_replicas),
        deployment(historical_base + "-cpu", "cpu-services", HISTORICAL_RELEASE,
                   expected_target_replicas),
    ]
    peers = [
        deployment(previous_base + "-cpu", "cpu-services", previous_id, {1}),
        deployment(historical_base, "ml-system", HISTORICAL_RELEASE, {1}),
    ]
    minimum_endpoints = 1 if restore else 2
    if (ready_endpoints(previous_base) < minimum_endpoints
            or ready_endpoints(historical_base) < minimum_endpoints):
        raise ValueError("both duplicate Service endpoints must be Ready before exchange")
    candidate = exact_candidate(state, coordinator_only=True)

    if restore:
        journal = (
            "workflow/capacity-windows/prompt-ab-cross-node-adapters-restore-"
            + champion_id + "-" + os.environ["BUILD_NUMBER"] + ".json"
        )
        store.client.put_object(
            Bucket=store.bucket,
            Key=journal,
            Body=json.dumps({
                "stage": "prompt_ab_cross_node_adapter_restore_intent",
                "build_url": os.environ["BUILD_URL"],
                "state_etag": etag,
                "route_revision": state["route_revision"],
                "champion_release_id": champion_id,
                "previous_release_id": previous_id,
                "historical_release_id": HISTORICAL_RELEASE,
                "targets": targets,
                "retained_peers": peers,
            }, sort_keys=True).encode(),
            ContentType="application/json",
            IfNoneMatch="*",
        )
        restore_target(previous_base, "ml-system", previous_id)
        restore_target(historical_base + "-cpu", "cpu-services", HISTORICAL_RELEASE)
        after, after_etag = store.read()
        if (
            after != state
            or after_etag != etag
            or ready_endpoints(previous_base) < 2
            or ready_endpoints(historical_base) < 2
            or not driver.verify_route(after, 0, after["route_revision"])
        ):
            raise ValueError("adapter restoration state/route/readiness verification failed")
        report = {
            "stage": "prompt_ab_cross_node_adapter_restore",
            "journal_key": journal,
            "restored": [obj["metadata"]["name"] for obj in targets],
            "state_unchanged": True,
            "route_unchanged": True,
            "inference_requests": 0,
        }
        Path(".llm-agent-cd").mkdir(exist_ok=True)
        Path(".llm-agent-cd/evaluation-preparation.json").write_text(
            json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
        return

    journal = (
        "workflow/capacity-windows/prompt-ab-cross-node-adapters-"
        + champion_id + "-" + os.environ["BUILD_NUMBER"] + ".json"
    )
    store.client.put_object(
        Bucket=store.bucket,
        Key=journal,
        Body=json.dumps({
            "stage": "prompt_ab_cross_node_adapter_exchange_intent",
            "build_url": os.environ["BUILD_URL"],
            "state_etag": etag,
            "route_revision": state["route_revision"],
            "champion_release_id": champion_id,
            "previous_release_id": previous_id,
            "historical_release_id": HISTORICAL_RELEASE,
            "candidate_release_id": candidate["release_id"],
            "targets": targets,
            "retained_peers": peers,
        }, sort_keys=True).encode(),
        ContentType="application/json",
        IfNoneMatch="*",
    )

    changed: list[tuple[str, str, str]] = []
    try:
        for obj, pool, release_id in (
            (targets[0], "ml-system", previous_id),
            (targets[1], "cpu-services", HISTORICAL_RELEASE),
        ):
            if obj["spec"].get("replicas") == 1:
                _scale_zero(obj)
                changed.append((obj["metadata"]["name"], pool, release_id))
        deployment(previous_base, "ml-system", previous_id, {0})
        deployment(historical_base + "-cpu", "cpu-services", HISTORICAL_RELEASE, {0})
        deployment(previous_base + "-cpu", "cpu-services", previous_id, {1})
        deployment(historical_base, "ml-system", HISTORICAL_RELEASE, {1})
        if ready_endpoints(previous_base) < 1 or ready_endpoints(historical_base) < 1:
            raise ValueError("retained adapter endpoint is not Ready")
        driver.preflight(
            state["champion"], candidate,
            json.loads(Path(FIXTURES).read_text()),
        )
        after, after_etag = store.read()
        if (
            after != state
            or after_etag != etag
            or not driver.verify_route(after, 0, after["route_revision"])
        ):
            raise ValueError("state or route changed during capacity exchange")
    except Exception:
        for name, pool, release_id in reversed(changed):
            restore_target(name, pool, release_id)
        raise

    report = {
        "stage": "prompt_ab_cross_node_adapter_exchange",
        "journal_key": journal,
        "champion_release_id": champion_id,
        "previous_release_id": previous_id,
        "historical_release_id": HISTORICAL_RELEASE,
        "candidate_release_id": candidate["release_id"],
        "scaled_to_zero": [obj["metadata"]["name"] for obj in targets],
        "retained_ready": [obj["metadata"]["name"] for obj in peers],
        "released_cpu_each_node": "25m",
        "exact_coordinator_candidate_capacity_verified": True,
        "state_unchanged": True,
        "route_unchanged": True,
        "inference_requests": 0,
    }
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(
        json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
