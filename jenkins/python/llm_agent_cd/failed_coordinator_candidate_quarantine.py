"""Quarantine the exact failed Coordinator-only candidate at zero traffic."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
import uuid

from apps.agentic.llm_ab_router.child_tasks import ChildTasks
from .coordinator_worker_capacity import coordinator_candidate
from .driver import Driver, command
from .evidence import task_of
from .manifests import name as resource_name
from .provision import kube
from .release_guard import check
from .state import StateStore
from .workflow import members
from .workflow_evidence import events


REVIEW = "reviewed-quarantine-coordinator-2c134-v2"
BASELINE = "3f470d14fc62799af915f9afee0c4ff4710eadecd6012c0ea1a0d13578dad658"
RELEASE = "2c134fb8697c8b78d12fd8fa2c9f75ab626d9a5b9d091f4d3d117b14aa94be05"
LLM_VERSION = "5f8c2912e51a1ad9ead09ba37f57aae046919dda389e2f619c6a15fe37549f63"
TIMEOUT_PROBES = (
    "candidate-coordinator-recommendation-a2a-v33",
    "candidate-coordinator-context-a2a-v33",
)
POOL_PROBE = "candidate-coordinator-composite-a2a-v33"


def _result(store: StateStore, probe: str) -> dict:
    key = "workflow/runtime-preflights/" + RELEASE + "/" + probe
    intent = json.loads(store.client.get_object(
        Bucket=store.bucket, Key=key + "/intent.json")["Body"].read())
    result = json.loads(store.client.get_object(
        Bucket=store.bucket, Key=key + "/result.json")["Body"].read())
    if (
        intent.get("inference_budget") != 1
        or result.get("release_id") != RELEASE
        or result.get("probe") != probe
        or result.get("verdict") != "FAIL"
        or result.get("source") != "infrastructure_test"
        or result.get("offline_requests") != 0
        or result.get("synthetic_requests") != 0
    ):
        raise ValueError("exact create-only failure evidence required: " + probe)
    return {"intent": intent, "result": result}


def _task_summary(candidate: dict, probe: str) -> dict:
    root = "workflow/runtime-preflights/" + RELEASE + "/" + probe
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, root))
    coordinator = "kagent__NS__" + resource_name(
        members(candidate)["coordinator"]).replace("-", "_")
    reader = ChildTasks("workflow-infrastructure-preflight", [coordinator])
    try:
        task = task_of(reader(request_id))
    finally:
        reader.close()
    return {
        "context_id": request_id,
        "task_id": task.get("id"),
        "state": task.get("status", {}).get("state"),
        "calls": len(events(task, "function_call")),
        "responses": len(events(task, "function_response")),
    }


def _adapter(name: str, pool: str) -> dict:
    value = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
    release_env = [entry for container in value["spec"]["template"]["spec"]["containers"]
        for entry in container.get("env", []) if entry.get("name") == "RELEASE_ID"]
    if (
        value["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
        or value["spec"].get("replicas") not in {0, 1}
        or value["spec"]["template"]["spec"].get("nodeSelector")
        != {"recsys.ai/pool": pool}
        or release_env != [{"name": "RELEASE_ID", "value": RELEASE}]
    ):
        raise ValueError("Coordinator-only adapter identity drift: " + name)
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    if parser.parse_args().image != REVIEW:
        raise ValueError("unreviewed Coordinator-only quarantine")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")

    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = json.loads(kube(
        "kagent", "get", "deployment", "recsys-workflow-router", "-o", "json"))
    os.environ.update(
        AB_SCOPE="workflow",
        AB_SECRET_NAME="recsys-workflow-runtime",
        AB_ALLOW_LIVE_TEST="true",
        AB_KAGENT_GRPC_TARGET="kagent-controller.kagent.svc.cluster.local:8084",
        AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0]["image"],
    )
    check("workflow")
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    if (
        state.get("phase") != "IDLE"
        or not state.get("activated")
        or state.get("experiment_id")
        or (state.get("champion") or {}).get("release_id") != BASELINE
    ):
        raise ValueError("exact IDLE baseline required")

    candidate = coordinator_candidate(state["champion"])
    if (
        candidate["release_id"] != RELEASE
        or candidate["llm_version_id"] != LLM_VERSION
        or candidate.get("change_scope") != "coordinator"
    ):
        raise ValueError("failed Coordinator-only candidate identity drift")
    pointers = {(state.get(key) or {}).get("release_id")
        for key in ("champion", "previous", "baseline", "pending")}
    if RELEASE in pointers or RELEASE in state.get("releases", {}):
        raise ValueError("failed candidate entered durable workflow state")

    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("baseline route is not verified")
    route = json.loads(kube(
        "kagent", "get", "virtualservice", "recsys-workflow-ab", "-o", "json"))
    if RELEASE in json.dumps(route["spec"]):
        raise ValueError("failed candidate unexpectedly appears in Istio route")
    with driver.db.connect() as connection:
        counts = {
            "sessions": connection.execute(
                "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s",
                (RELEASE,)).fetchone()["n"],
            "invocations": connection.execute(
                "SELECT count(*) n FROM recsys_ab.invocations WHERE release_id=%s",
                (RELEASE,)).fetchone()["n"],
            "unfinished_all": connection.execute(
                "SELECT count(*) n FROM recsys_ab.invocations WHERE finished_at IS NULL"
            ).fetchone()["n"],
            "active_dispatches": connection.execute(
                "SELECT count(*) n FROM recsys_ab.trigger_requests "
                "WHERE status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED','RUNNING')"
            ).fetchone()["n"],
        }
    if any(counts.values()):
        raise ValueError("routing DB activity blocks candidate quarantine")

    evidence = {probe: _result(store, probe) for probe in (*TIMEOUT_PROBES, POOL_PROBE)}
    for probe in TIMEOUT_PROBES:
        result = evidence[probe]["result"]
        if (result.get("reason") != "timeout_or_transport_failure_no_retry"
                or result.get("error_type") != "ReadTimeout"):
            raise ValueError("exact Coordinator timeout required: " + probe)
    pool_error = evidence[POOL_PROBE]["result"].get("evidence", {}).get("error", {})
    if (
        pool_error.get("code") != -32603
        or "substrate worker pool has no free workers" not in pool_error.get("message", "")
    ):
        raise ValueError("exact post-timeout two-worker exhaustion evidence required")

    task_before = {probe: _task_summary(candidate, probe) for probe in TIMEOUT_PROBES}
    base = "rec-ab-" + RELEASE[:20]
    adapters = (_adapter(base, "ml-system"), _adapter(base + "-cpu", "cpu-services"))
    coordinator = resource_name(members(candidate)["coordinator"])
    agent = json.loads(kube("kagent", "get", "sandboxagent", coordinator, "-o", "json"))
    model = json.loads(kube("kagent", "get", "modelconfig", coordinator, "-o", "json"))
    templates = json.loads(kube("kagent", "get", "actortemplate", "-l",
        "kagent.dev/sandbox-agent=" + coordinator, "-o", "json"))["items"]
    if (
        agent["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
        or model["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
        or len(templates) != 1
    ):
        raise ValueError("failed Coordinator resource ownership drift")
    backend_name = "rec-llm-" + LLM_VERSION[:20]
    backend = json.loads(kube("kagent", "get", "deployment", backend_name, "-o", "json"))
    if (backend["spec"].get("replicas") != 1
            or backend.get("status", {}).get("readyReplicas") != 1):
        raise ValueError("candidate backend must remain Ready during quarantine")

    journal = "workflow/capacity-windows/failed-coordinator-candidate-" \
        + RELEASE[:20] + "-" + os.environ["BUILD_NUMBER"] + ".json"
    store.client.put_object(Bucket=store.bucket, Key=journal,
        Body=json.dumps({"stage": "failed_coordinator_candidate_quarantine_intent",
            "build_url": os.environ["BUILD_URL"], "state_etag": etag,
            "candidate": candidate, "probe_evidence": evidence,
            "task_before": task_before, "adapters": adapters,
            "coordinator_agent": agent, "coordinator_model": model,
            "coordinator_templates": templates, "retained_backend": backend,
            "counts": counts}, sort_keys=True).encode(),
        ContentType="application/json", IfNoneMatch="*")

    selectors = [value["spec"]["selector"]["matchLabels"] for value in adapters]
    for value in adapters:
        if value["spec"].get("replicas") == 1:
            kube("kagent", "patch", "deployment", value["metadata"]["name"],
                "--type=json", "-p", json.dumps([
                    {"op": "test", "path": "/metadata/resourceVersion",
                     "value": value["metadata"]["resourceVersion"]},
                    {"op": "test", "path": "/spec/replicas", "value": 1},
                    {"op": "replace", "path": "/spec/replicas", "value": 0}]))
    kube("kagent", "delete", "sandboxagent", coordinator,
        "--wait=true", "--timeout=180s")
    deadline = time.monotonic() + 180
    while True:
        pods = json.loads(command("kubectl", "-n", "kagent", "get", "pods", "-o", "json"))["items"]
        admitted = [pod for pod in pods
            if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            and any(all(pod["metadata"].get("labels", {}).get(k) == v
                for k, v in selector.items()) for selector in selectors)]
        live_templates = kube("kagent", "get", "actortemplate", "-l",
            "kagent.dev/sandbox-agent=" + coordinator, "-o", "name").strip()
        if not admitted and not live_templates:
            break
        if time.monotonic() > deadline:
            raise ValueError("HOLD candidate did not quiesce; no force delete")
        time.sleep(3)

    first = {probe: _task_summary(candidate, probe) for probe in TIMEOUT_PROBES}
    time.sleep(10)
    second = {probe: _task_summary(candidate, probe) for probe in TIMEOUT_PROBES}
    after, after_etag = store.read()
    backend_after = json.loads(kube(
        "kagent", "get", "deployment", backend_name, "-o", "json"))
    if (
        first != second
        or after != state
        or after_etag != etag
        or backend_after["spec"].get("replicas") != 1
        or backend_after.get("status", {}).get("readyReplicas") != 1
        or not driver.verify_route(after, 0, after["route_revision"])
    ):
        raise ValueError("quiescence/backend/state/route verification failed")

    report = {
        "stage": "failed_coordinator_candidate_quarantine",
        "candidate_release_id": RELEASE,
        "candidate_llm_version_id": LLM_VERSION,
        "journal_key": journal,
        "adapters_scaled_to_zero": [v["metadata"]["name"] for v in adapters],
        "coordinator_sandboxagent_removed": coordinator,
        "coordinator_modelconfig_retained": coordinator,
        "specialist_agents_retained": True,
        "llm_backend_retained_ready": True,
        "taskstore_and_minio_evidence_retained": True,
        "tasks_quiescent": second,
        "state_unchanged": True,
        "route_unchanged": True,
        "canary_started": False,
        "online_ab_requests": 0,
        "inference_requests": 0,
    }
    target = Path(".llm-agent-cd/evaluation-preparation.json")
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
