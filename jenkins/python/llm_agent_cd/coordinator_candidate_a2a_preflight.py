"""Deploy and probe an unrouted Coordinator-only Qwen2.5 candidate.

Both candidate specialist agents retain the baseline LLM, generation config,
prompt and tools.  The three create-only calls exercise only the changed
Coordinator model as the experiment axis.  No experiment or traffic change is
created by this job.
"""
import argparse
import json
import os
from pathlib import Path

from .coordinator_worker_capacity import coordinator_candidate
from .driver import Driver, command
from .release_guard import check
from .runtime_probe import readonly_candidate_a2a_probes
from .state import StateStore
from .workflow import members, prompt_checksum
from .workflow_contract import revise_coordinator_native_sequential_baseline


WINDOW = "qwen25-coordinator-only-native-isolated-sequential-a2a-v5"


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
        raise ValueError("unreviewed Coordinator-only preflight window")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")

    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router_image = json.loads(command(
        "kubectl", "-n", "kagent", "get", "deployment",
        "recsys-workflow-router", "-o", "json"
    ))["spec"]["template"]["spec"]["containers"][0]["image"]
    os.environ.update(
        AB_SCOPE="workflow",
        AB_SECRET_NAME="recsys-workflow-runtime",
        AB_ALLOW_LIVE_TEST="true",
        AB_ROUTER_IMAGE=router_image,
    )
    check("workflow")
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    if (
        state.get("phase") != "IDLE"
        or not state.get("activated")
        or state.get("experiment_id")
    ):
        raise ValueError("exact activated IDLE baseline required")
    previous = state.get("previous")
    if not previous:
        raise ValueError("prompt-migration parent release is required")
    expected_baseline, migration_audit = revise_coordinator_native_sequential_baseline(previous)
    if (
        state["champion"] != expected_baseline
        or state.get("baseline") != expected_baseline
        or state.get("pending") != expected_baseline
        or state.get("baseline_cutover", {}).get("migration_kind")
        != migration_audit["change_type"]
    ):
        raise ValueError("exact activated Coordinator prompt baseline required")

    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("baseline route is not verified at candidate weight zero")
    if any(_active_work(driver)):
        raise ValueError("active invocation or dispatch blocks Coordinator preflight")
    scaled = json.loads(command(
        "kubectl", "-n", "kagent", "get", "scaledobject",
        "recsys-coordinator-sandbox-pool", "-o", "json"))
    worker = json.loads(command(
        "kubectl", "-n", "kagent", "get", "workerpool",
        "recsys-coordinator-sandbox-pool", "-o", "json"))
    if (
        (scaled["spec"].get("minReplicaCount"), scaled["spec"].get("maxReplicaCount"))
        != (2, 2)
        or worker["spec"].get("replicas") != 2
        or worker.get("status", {}).get("replicas") != 2
    ):
        raise ValueError("two Ready Coordinator workers are required")

    candidate = coordinator_candidate(state["champion"])
    frozen_prompt_checksum = prompt_checksum(state["champion"])
    if (
        frozen_prompt_checksum != migration_audit["prompt_checksum"]
        or prompt_checksum(candidate) != frozen_prompt_checksum
        or candidate["agents"] != state["champion"]["agents"]
        or any("isolateSessions" in tool for tool in
               state["champion"]["agents"]["coordinator"]["tools"])
        or any("isolateSessions" in tool for tool in
               candidate["agents"]["coordinator"]["tools"])
    ):
        raise ValueError("control/candidate native sequential baseline is not frozen")
    control_members, candidate_members = members(state["champion"]), members(candidate)
    if candidate.get("change_scope") != "coordinator":
        raise ValueError("candidate is not Coordinator-only")
    for role in ("context", "recommendation"):
        if (
            control_members[role]["llm_version_id"]
            != candidate_members[role]["llm_version_id"]
            or control_members[role]["config_id"]
            != candidate_members[role]["config_id"]
        ):
            raise ValueError("specialist experiment axis drift: " + role)
    if (
        control_members["coordinator"]["llm_version_id"]
        == candidate_members["coordinator"]["llm_version_id"]
    ):
        raise ValueError("Coordinator LLM did not change")

    fixtures = json.loads(Path(
        "configs/llm-ab/workflow-cases-v11.json").read_text())
    driver.preflight(state["champion"], candidate, fixtures)
    driver.deploy(candidate)
    driver.verify_release(candidate)

    before_probes, before_etag = store.read()
    if (
        before_probes != state
        or before_etag != etag
        or not driver.verify_route(before_probes, 0, before_probes["route_revision"])
    ):
        raise ValueError("state or route changed before Coordinator probes")

    reports = readonly_candidate_a2a_probes(
        store, candidate, candidate["runtime"]["go_adk_image"])

    after, after_etag = store.read()
    if (
        after != state
        or after_etag != etag
        or any(_active_work(driver))
        or not driver.verify_route(after, 0, after["route_revision"])
    ):
        raise ValueError("state, route, or durable work changed during Coordinator preflight")

    report = {
        "stage": "coordinator_candidate_a2a_preflight",
        "window": WINDOW,
        "baseline_release_id": state["champion"]["release_id"],
        "candidate_release_id": candidate["release_id"],
        "candidate_llm_version_id": candidate["llm_version_id"],
        "change_scope": "coordinator",
        "prompt_checksum": frozen_prompt_checksum,
        "control_candidate_prompt_checksum_equal": True,
        "control_candidate_isolate_sessions": True,
        "native_go_sandbox_agent": True,
        "builtin_a2a_prompt_included": False,
        "specialist_llms_unchanged": True,
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
