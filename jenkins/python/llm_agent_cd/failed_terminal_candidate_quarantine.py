"""Quarantine the exact failed terminal-template candidate at zero weight.

The first create-only probe timed out and occupied the sole reviewed
Coordinator worker; the following two create-only probes were rejected before
execution because that pool had no free worker.  This action stops only the
candidate Coordinator actor and stateless adapters.  It preserves the model
backend, specialist agents, ModelConfigs, TaskStore history and immutable
MinIO evidence for diagnosis.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import time
import uuid

from apps.agentic.llm_ab_router.child_tasks import ChildTasks
from apps.agentic.llm_ab_router.trigger import candidate_from_config
from .driver import Driver, command
from .evidence import task_of
from .manifests import name as resource_name
from .provision import kube
from .release import digest
from .release_guard import check
from .state import StateStore
from .workflow import members
from .workflow_evidence import events


REVIEW = "reviewed-quarantine-d406fa-terminal-v1"
BASELINE = "2458bfb95a8e3310adeea29ab39fe2f5ebf963d4f48060356fdc8c8fb84385c6"
RELEASE = "d406faebb76eff219b3995107c6975e57ea337a9b60a709da4e40a4e842acc9e"
LLM_VERSION = "5f8c2912e51a1ad9ead09ba37f57aae046919dda389e2f619c6a15fe37549f63"
CATALOG = "configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m-tools-v3.json"
PRIMARY_PROBE = "candidate-coordinator-recommendation-a2a-v29"
POOL_PROBES = (
    "candidate-coordinator-context-a2a-v29",
    "candidate-coordinator-composite-a2a-v29",
)


def expected_candidate(state: dict) -> dict:
    llm = json.loads(Path(CATALOG).read_text())
    config = {
        "schema_version": 1,
        "scope": "workflow",
        "baseline_workflow_release_id": BASELINE,
        "global_generation": deepcopy(state["champion"]["global_generation"]),
        "llm_release_ref": digest(llm),
        "experiment_type": "llm_only",
        "policy_ref": "workflow-live-test",
    }
    candidate = candidate_from_config(state["champion"], config, llm)
    if candidate["release_id"] != RELEASE or candidate["llm_version_id"] != LLM_VERSION:
        raise ValueError("failed terminal candidate identity drift")
    return candidate


def _task_summary(candidate: dict) -> dict:
    probe_root = "workflow/runtime-preflights/" + RELEASE + "/" + PRIMARY_PROBE
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, probe_root))
    coordinator = "kagent__NS__" + resource_name(
        members(candidate)["coordinator"]
    ).replace("-", "_")
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


def _deployment(name: str, pool: str) -> dict:
    value = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
    env = [
        entry
        for container in value["spec"]["template"]["spec"]["containers"]
        for entry in container.get("env", [])
        if entry.get("name") == "RELEASE_ID"
    ]
    if (
        value["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
        or value["spec"].get("replicas") not in {0, 1}
        or value["spec"]["template"]["spec"].get("nodeSelector")
        != {"recsys.ai/pool": pool}
        or env != [{"name": "RELEASE_ID", "value": RELEASE}]
    ):
        raise ValueError("failed terminal candidate adapter identity drift")
    return value


def _read_result(store: StateStore, probe: str) -> tuple[dict, dict]:
    root = "workflow/runtime-preflights/" + RELEASE + "/" + probe
    intent = json.loads(
        store.client.get_object(Bucket=store.bucket, Key=root + "/intent.json")[
            "Body"
        ].read()
    )
    result = json.loads(
        store.client.get_object(Bucket=store.bucket, Key=root + "/result.json")[
            "Body"
        ].read()
    )
    if (
        intent.get("inference_budget") != 1
        or result.get("verdict") != "FAIL"
        or result.get("release_id") != RELEASE
        or result.get("probe") != probe
        or result.get("source") != "infrastructure_test"
        or result.get("offline_requests") != 0
        or result.get("synthetic_requests") != 0
    ):
        raise ValueError("exact failed create-only probe evidence required")
    return intent, result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    if parser.parse_args().image != REVIEW:
        raise ValueError("unreviewed failed terminal candidate quarantine")
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
        raise ValueError("exact post-preflight IDLE baseline required")
    protected = {
        (state.get(key) or {}).get("release_id")
        for key in ("champion", "previous", "baseline", "pending")
    }
    if RELEASE in protected or RELEASE in state.get("releases", {}):
        raise ValueError("failed terminal candidate entered workflow state")

    candidate = expected_candidate(state)
    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("baseline route is not verified")
    route = json.loads(
        kube("kagent", "get", "virtualservice", "recsys-workflow-ab", "-o", "json")
    )
    if RELEASE in json.dumps(route["spec"]):
        raise ValueError("failed terminal candidate unexpectedly appears in Istio routing")

    with driver.db.connect() as connection:
        counts = {
            "sessions": connection.execute(
                "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s",
                (RELEASE,),
            ).fetchone()["n"],
            "invocations": connection.execute(
                "SELECT count(*) n FROM recsys_ab.invocations WHERE release_id=%s",
                (RELEASE,),
            ).fetchone()["n"],
            "unfinished_all": connection.execute(
                "SELECT count(*) n FROM recsys_ab.invocations WHERE finished_at IS NULL"
            ).fetchone()["n"],
            "active_dispatches": connection.execute(
                "SELECT count(*) n FROM recsys_ab.trigger_requests "
                "WHERE status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED','RUNNING')"
            ).fetchone()["n"],
        }
    if any(counts.values()):
        raise ValueError("routing DB activity blocks failed-candidate quarantine")

    primary_intent, primary = _read_result(store, PRIMARY_PROBE)
    if (
        primary.get("reason") != "timeout_or_transport_failure_no_retry"
        or primary.get("error_type") != "ReadTimeout"
    ):
        raise ValueError("exact primary candidate timeout evidence required")
    evidence = {PRIMARY_PROBE: {"intent": primary_intent, "result": primary}}
    for probe in POOL_PROBES:
        intent, result = _read_result(store, probe)
        error = result.get("evidence", {}).get("error", {})
        if (
            error.get("code") != -32603
            or "substrate worker pool has no free workers" not in error.get("message", "")
        ):
            raise ValueError("exact post-timeout worker exhaustion evidence required")
        evidence[probe] = {"intent": intent, "result": result}

    before = _task_summary(candidate)
    base = "rec-ab-" + RELEASE[:20]
    adapters = (
        _deployment(base, "ml-system"),
        _deployment(base + "-cpu", "cpu-services"),
    )
    variants = members(candidate)
    coordinator = resource_name(variants["coordinator"])
    coordinator_agent = json.loads(
        kube("kagent", "get", "sandboxagent", coordinator, "-o", "json")
    )
    coordinator_model = json.loads(
        kube("kagent", "get", "modelconfig", coordinator, "-o", "json")
    )
    templates = json.loads(
        kube(
            "kagent",
            "get",
            "actortemplate",
            "-l",
            "kagent.dev/sandbox-agent=" + coordinator,
            "-o",
            "json",
        )
    )["items"]
    if (
        coordinator_agent["metadata"].get("labels", {}).get("recsys.ai/owner")
        != "llm-agent-cd"
        or coordinator_model["metadata"].get("labels", {}).get("recsys.ai/owner")
        != "llm-agent-cd"
        or len(templates) != 1
    ):
        raise ValueError("failed terminal candidate Coordinator ownership drift")

    backend_name = "rec-llm-" + LLM_VERSION[:20]
    backend = json.loads(
        kube("kagent", "get", "deployment", backend_name, "-o", "json")
    )
    if (
        backend["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
        or backend["spec"].get("replicas") != 1
        or backend.get("status", {}).get("readyReplicas") != 1
    ):
        raise ValueError("failed terminal candidate backend is not Ready")

    journal = (
        "workflow/capacity-windows/failed-terminal-candidate-quarantine-"
        + RELEASE[:20]
        + "-"
        + os.environ["BUILD_NUMBER"]
        + ".json"
    )
    store.client.put_object(
        Bucket=store.bucket,
        Key=journal,
        Body=json.dumps(
            {
                "stage": "failed_terminal_candidate_quarantine_intent",
                "build_url": os.environ["BUILD_URL"],
                "state_etag": etag,
                "baseline_release_id": BASELINE,
                "candidate": candidate,
                "probe_evidence": evidence,
                "primary_task_before": before,
                "adapters": adapters,
                "coordinator_agent": coordinator_agent,
                "coordinator_model_config": coordinator_model,
                "coordinator_actor_templates": templates,
                "retained_backend": backend,
                "counts": counts,
            },
            sort_keys=True,
        ).encode(),
        ContentType="application/json",
        IfNoneMatch="*",
    )

    selectors = [value["spec"]["selector"]["matchLabels"] for value in adapters]
    for value in adapters:
        if value["spec"].get("replicas") == 1:
            kube(
                "kagent",
                "patch",
                "deployment",
                value["metadata"]["name"],
                "--type=json",
                "-p",
                json.dumps(
                    [
                        {
                            "op": "test",
                            "path": "/metadata/resourceVersion",
                            "value": value["metadata"]["resourceVersion"],
                        },
                        {"op": "test", "path": "/spec/replicas", "value": 1},
                        {"op": "replace", "path": "/spec/replicas", "value": 0},
                    ]
                ),
            )
    kube(
        "kagent",
        "delete",
        "sandboxagent",
        coordinator,
        "--wait=true",
        "--timeout=180s",
    )
    deadline = time.monotonic() + 180
    while True:
        pods = json.loads(
            command("kubectl", "-n", "kagent", "get", "pods", "-o", "json")
        )["items"]
        admitted = [
            pod
            for pod in pods
            if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            and any(
                all(
                    pod["metadata"].get("labels", {}).get(key) == wanted
                    for key, wanted in selector.items()
                )
                for selector in selectors
            )
        ]
        live_templates = kube(
            "kagent",
            "get",
            "actortemplate",
            "-l",
            "kagent.dev/sandbox-agent=" + coordinator,
            "-o",
            "name",
        ).strip()
        if not admitted and not live_templates:
            break
        if time.monotonic() > deadline:
            raise ValueError("HOLD failed terminal candidate did not quiesce")
        time.sleep(3)

    first = _task_summary(candidate)
    time.sleep(10)
    second = _task_summary(candidate)
    after, after_etag = store.read()
    backend_after = json.loads(
        kube("kagent", "get", "deployment", backend_name, "-o", "json")
    )
    if (
        first != second
        or backend_after["spec"].get("replicas") != 1
        or backend_after.get("status", {}).get("readyReplicas") != 1
        or after != state
        or after_etag != etag
        or not driver.verify_route(after, 0, after["route_revision"])
    ):
        raise ValueError("candidate quiescence/backend/state/route verification failed")

    report = {
        "stage": "failed_terminal_candidate_quarantine",
        "candidate_release_id": RELEASE,
        "candidate_llm_version_id": LLM_VERSION,
        "journal_key": journal,
        "adapters_scaled_to_zero": [value["metadata"]["name"] for value in adapters],
        "coordinator_sandboxagent_removed": coordinator,
        "coordinator_modelconfig_retained": coordinator,
        "specialist_agents_retained": True,
        "llm_backend_retained_ready": True,
        "taskstore_and_minio_evidence_retained": True,
        "primary_task_quiescent": second,
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
