"""Quarantine the failed frozen-prompt Coordinator candidate at zero traffic."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

from .coordinator_worker_capacity import coordinator_candidate
from .driver import Driver, command
from .evidence import task_of
from .manifests import name as resource_name
from .provision import kube
from .release_guard import check
from .state import StateStore
from .workflow import members
from .workflow_contract import revise_coordinator_terminal_prompt
from .workflow_evidence import events


REVIEW = "reviewed-quarantine-frozen-prompt-candidate-v1"
PROBES = {
    "recommendation": "candidate-coordinator-recommendation-a2a-v30",
    "context": "candidate-coordinator-context-a2a-v30",
    "composite": "candidate-coordinator-composite-a2a-v30",
}


def _adapter(name: str, pool: str, release_id: str) -> dict:
    value = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
    release_env = [entry for container in value["spec"]["template"]["spec"]["containers"]
        for entry in container.get("env", []) if entry.get("name") == "RELEASE_ID"]
    if (
        value["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
        or value["spec"].get("replicas") not in {0, 1}
        or value["spec"]["template"]["spec"].get("nodeSelector") != {"recsys.ai/pool": pool}
        or release_env != [{"name": "RELEASE_ID", "value": release_id}]
    ):
        raise ValueError("failed prompt candidate adapter identity drift: " + name)
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    if parser.parse_args().image != REVIEW:
        raise ValueError("unreviewed prompt-candidate quarantine")
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
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    if state.get("phase") != "IDLE" or not state.get("activated") or state.get("experiment_id"):
        raise ValueError("exact activated IDLE baseline required")
    expected, audit = revise_coordinator_terminal_prompt(state.get("previous") or {})
    if (
        state.get("champion") != expected
        or state.get("baseline_cutover", {}).get("migration_kind") != audit["change_type"]
    ):
        raise ValueError("exact frozen-prompt baseline required")
    candidate = coordinator_candidate(state["champion"])
    release_id = candidate["release_id"]
    pointers = {(state.get(key) or {}).get("release_id")
                for key in ("champion", "previous", "baseline", "pending")}
    if release_id in pointers or release_id in state.get("releases", {}):
        raise ValueError("failed candidate entered durable workflow state")

    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("baseline route is not verified")
    route = json.loads(kube(
        "kagent", "get", "virtualservice", "recsys-workflow-ab", "-o", "json"))
    if release_id in json.dumps(route["spec"]):
        raise ValueError("failed candidate unexpectedly appears in Istio route")
    with driver.db.connect() as connection:
        counts = {
            "sessions": connection.execute(
                "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s",
                (release_id,)).fetchone()["n"],
            "invocations": connection.execute(
                "SELECT count(*) n FROM recsys_ab.invocations WHERE release_id=%s",
                (release_id,)).fetchone()["n"],
            "unfinished_all": connection.execute(
                "SELECT count(*) n FROM recsys_ab.invocations WHERE finished_at IS NULL"
            ).fetchone()["n"],
            "active_dispatches": connection.execute(
                "SELECT count(*) n FROM recsys_ab.trigger_requests "
                "WHERE status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED','RUNNING')"
            ).fetchone()["n"],
        }
    if any(counts.values()):
        raise ValueError("routing DB activity blocks failed prompt candidate quarantine")

    summaries = {}
    for label, probe in PROBES.items():
        root = "workflow/runtime-preflights/" + release_id + "/" + probe
        intent = json.loads(store.client.get_object(
            Bucket=store.bucket, Key=root + "/intent.json")["Body"].read())
        result = json.loads(store.client.get_object(
            Bucket=store.bucket, Key=root + "/result.json")["Body"].read())
        if (
            intent.get("inference_budget") != 1
            or result.get("release_id") != release_id
            or result.get("probe") != probe
            or result.get("verdict") != "FAIL"
            or result.get("source") != "infrastructure_test"
            or result.get("offline_requests") != 0
            or result.get("synthetic_requests") != 0
        ):
            raise ValueError("exact create-only v30 failure evidence required: " + label)
        summary = {key: result.get(key) for key in ("verdict", "reason", "error_type")}
        if label == "context":
            task = task_of(result.get("evidence", {}))
            calls, responses = events(task, "function_call"), events(task, "function_response")
            if (
                task.get("status", {}).get("state") != "TASK_STATE_INPUT_REQUIRED"
                or len(calls) != len(responses)
                or not calls
                or len(calls) < 2
                or len({call.get("name") for call in calls}) != 1
            ):
                raise ValueError("expected repeated Context route evidence")
            summary.update(state="TASK_STATE_INPUT_REQUIRED", calls=len(calls), responses=len(responses))
        elif result.get("reason") != "timeout_or_transport_failure_no_retry":
            raise ValueError("expected no-retry timeout evidence: " + label)
        summaries[label] = summary

    base = "rec-ab-" + release_id[:20]
    adapters = (_adapter(base, "ml-system", release_id),
                _adapter(base + "-cpu", "cpu-services", release_id))
    coordinator = resource_name(members(candidate)["coordinator"])
    agent = json.loads(kube("kagent", "get", "sandboxagent", coordinator, "-o", "json"))
    if agent["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd":
        raise ValueError("failed candidate Coordinator ownership drift")
    backend_name = "rec-llm-" + members(candidate)["coordinator"]["llm_version_id"][:20]
    backend = json.loads(kube("kagent", "get", "deployment", backend_name, "-o", "json"))
    if backend["spec"].get("replicas") != 1 or backend.get("status", {}).get("readyReplicas") != 1:
        raise ValueError("candidate backend must remain Ready")

    journal = "workflow/capacity-windows/failed-prompt-candidate-" \
        + release_id + "-" + os.environ["BUILD_NUMBER"] + ".json"
    store.client.put_object(
        Bucket=store.bucket,
        Key=journal,
        Body=json.dumps({
            "stage": "failed_prompt_candidate_quarantine_intent",
            "build_url": os.environ["BUILD_URL"],
            "state_etag": etag,
            "candidate": candidate,
            "probe_summaries": summaries,
            "adapters": adapters,
            "coordinator": agent,
            "retained_backend": backend,
            "counts": counts,
        }, sort_keys=True).encode(),
        ContentType="application/json",
        IfNoneMatch="*",
    )

    selectors = [value["spec"]["selector"]["matchLabels"] for value in adapters]
    for value in adapters:
        if value["spec"].get("replicas") == 1:
            kube("kagent", "patch", "deployment", value["metadata"]["name"],
                "--type=json", "-p", json.dumps([
                    {"op": "test", "path": "/metadata/resourceVersion",
                     "value": value["metadata"]["resourceVersion"]},
                    {"op": "test", "path": "/spec/replicas", "value": 1},
                    {"op": "replace", "path": "/spec/replicas", "value": 0},
                ]))
    kube("kagent", "delete", "sandboxagent", coordinator,
         "--wait=true", "--timeout=180s")
    deadline = time.monotonic() + 180
    while True:
        pods = json.loads(command(
            "kubectl", "-n", "kagent", "get", "pods", "-o", "json"))["items"]
        admitted = [pod for pod in pods
            if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            and any(all(pod["metadata"].get("labels", {}).get(key) == value
                        for key, value in selector.items()) for selector in selectors)]
        templates = kube("kagent", "get", "actortemplate", "-l",
            "kagent.dev/sandbox-agent=" + coordinator, "-o", "name").strip()
        if not admitted and not templates:
            break
        if time.monotonic() > deadline:
            raise ValueError("HOLD failed candidate did not quiesce; no force delete")
        time.sleep(3)

    after, after_etag = store.read()
    backend_after = json.loads(kube(
        "kagent", "get", "deployment", backend_name, "-o", "json"))
    if (
        after != state
        or after_etag != etag
        or backend_after.get("status", {}).get("readyReplicas") != 1
        or not driver.verify_route(after, 0, after["route_revision"])
    ):
        raise ValueError("quarantine state/route/backend verification failed")

    report = {
        "stage": "failed_prompt_candidate_quarantine",
        "candidate_release_id": release_id,
        "journal_key": journal,
        "probe_summaries": summaries,
        "adapters_scaled_to_zero": [value["metadata"]["name"] for value in adapters],
        "coordinator_sandboxagent_removed": coordinator,
        "specialist_agents_retained": True,
        "llm_backend_retained_ready": True,
        "state_unchanged": True,
        "route_unchanged": True,
        "online_ab_requests": 0,
        "inference_requests": 0,
    }
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(
        json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
