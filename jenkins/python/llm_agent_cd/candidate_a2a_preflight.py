"""Deploy and probe the exact unrouted Qwen2.5 workflow candidate.

This job runs under the common production/state locks.  It never creates an
experiment, changes Istio allocation, or contributes to offline/online sample
counts.  Every inference is create-only and is persisted before SendMessage.
"""
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path

from apps.agentic.llm_ab_router.trigger import candidate_from_config
from .driver import Driver, command
from .release import digest
from .release_guard import check
from .runtime_probe import readonly_candidate_a2a_probes
from .state import StateStore


WINDOW = "qwen25-stock-terminal-full-a2a-v2"
CATALOG = "configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m-tools-v3.json"
FIXTURES = "configs/llm-ab/workflow-cases-v11.json"


def _active_dispatches(driver):
    with driver.db.connect() as connection:
        unfinished = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations WHERE finished_at IS NULL"
        ).fetchone()["n"]
        dispatching = connection.execute("""SELECT count(*) n
            FROM recsys_ab.trigger_requests
            WHERE status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED','RUNNING')""").fetchone()["n"]
    return unfinished, dispatching


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True,
                        help="Exact operator-reviewed preflight window ID")
    args = parser.parse_args()
    if args.image != WINDOW:
        raise ValueError("unreviewed candidate A2A preflight window")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")

    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    os.environ.update(
        AB_SCOPE="workflow",
        AB_SECRET_NAME="recsys-workflow-runtime",
        AB_ALLOW_LIVE_TEST="true",
        AB_ROUTER_IMAGE=json.loads(command(
            "kubectl", "-n", "kagent", "get", "deployment",
            "recsys-workflow-router", "-o", "json"
        ))["spec"]["template"]["spec"]["containers"][0]["image"],
    )
    check("workflow")
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    if (state.get("phase") != "IDLE" or not state.get("activated")
            or state.get("experiment_id")):
        raise ValueError("activated IDLE workflow with no experiment required")

    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("baseline route is not verified at candidate weight zero")
    unfinished, dispatching = _active_dispatches(driver)
    if unfinished or dispatching:
        raise ValueError("active invocation or dispatch blocks candidate preflight")

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
    candidate = candidate_from_config(state["champion"], config, llm)
    fixtures = json.loads(Path(FIXTURES).read_text())
    driver.preflight(state["champion"], candidate, fixtures)
    driver.deploy(candidate)
    driver.verify_release(candidate)

    # Deployment and readiness must not implicitly change the release state or
    # allocation.  Check immediately before the first create-only inference.
    before_probes, before_etag = store.read()
    if (before_probes != state or before_etag != etag
            or not driver.verify_route(before_probes, 0,
                                       before_probes["route_revision"])):
        raise ValueError("state or route changed before candidate A2A probes")

    reports = readonly_candidate_a2a_probes(
        store, candidate, candidate["runtime"]["go_adk_image"])

    after, after_etag = store.read()
    unfinished, dispatching = _active_dispatches(driver)
    if (after != state or after_etag != etag
            or unfinished or dispatching
            or not driver.verify_route(after, 0, after["route_revision"])):
        raise ValueError("state, route, or durable work changed during candidate preflight")

    report = {
        "stage": "candidate_full_a2a_preflight",
        "window": WINDOW,
        "baseline_release_id": state["champion"]["release_id"],
        "candidate_release_id": candidate["release_id"],
        "candidate_llm_version_id": candidate["llm_version_id"],
        "verdict": "PASS",
        "runtime_preflights": reports,
        "inference_requests": 3,
        "offline_requests": 0,
        "synthetic_requests": 0,
        "state_unchanged": True,
        "route_unchanged": True,
        "candidate_weight": 0,
        "build_url": os.environ["BUILD_URL"],
    }
    target = Path(".llm-agent-cd/evaluation-preparation.json")
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
