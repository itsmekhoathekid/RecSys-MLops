"""Contain one never-routed candidate whose stock Coordinator loops on A2A results.

The exact candidate is absent from workflow state and Istio routing.  Preserve
its manifests and immutable probe evidence, scale only its stateless adapters,
and remove only its Coordinator SandboxAgent so the Substrate actor cannot
continue executing duplicate specialist calls.  ModelConfig, specialists,
backend, TaskStore, and MinIO evidence remain available.
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


REVIEW = "reviewed-quarantine-c6bf33-stock-loop-v1"
BASELINE = "2458bfb95a8e3310adeea29ab39fe2f5ebf963d4f48060356fdc8c8fb84385c6"
RELEASE = "c6bf33cb156b4ff28cf322e7db87315b78e6f6d77f5f966eff20d737659b411c"
CATALOG = "configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m-tools-v2.json"
PROBES = (
    "candidate-coordinator-recommendation-a2a-v29",
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
    if candidate["release_id"] != RELEASE:
        raise ValueError("failed candidate identity drift")
    return candidate


def trajectory(candidate: dict) -> dict:
    agent = "kagent__NS__" + resource_name(members(candidate)["coordinator"]).replace(
        "-", "_"
    )
    reader = ChildTasks("workflow-infrastructure-preflight", [agent])
    output = {}
    try:
        for probe in PROBES:
            root = "workflow/runtime-preflights/" + RELEASE + "/" + probe
            request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, root))
            task = task_of(reader(request_id))
            output[probe] = {
                "context_id": request_id,
                "task_id": task.get("id"),
                "state": task.get("status", {}).get("state"),
                "calls": len(events(task, "function_call")),
                "responses": len(events(task, "function_response")),
            }
    finally:
        reader.close()
    return output


def _deployment(name: str, pool: str) -> dict:
    obj = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
    env = [
        entry
        for container in obj["spec"]["template"]["spec"]["containers"]
        for entry in container.get("env", [])
        if entry["name"] == "RELEASE_ID"
    ]
    if (
        obj["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
        or obj["spec"].get("replicas") not in {0, 1}
        or obj["spec"]["template"]["spec"].get("nodeSelector")
        != {"recsys.ai/pool": pool}
        or env != [{"name": "RELEASE_ID", "value": RELEASE}]
    ):
        raise ValueError("failed candidate adapter identity drift")
    return obj


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    if parser.parse_args().image != REVIEW:
        raise ValueError("unreviewed failed candidate loop quarantine")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")

    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    os.environ.update(
        AB_SCOPE="workflow",
        AB_SECRET_NAME="recsys-workflow-runtime",
        AB_ALLOW_LIVE_TEST="true",
        AB_KAGENT_GRPC_TARGET="kagent-controller.kagent.svc.cluster.local:8084",
        AB_ROUTER_IMAGE=json.loads(
            kube("kagent", "get", "deployment", "recsys-workflow-router", "-o", "json")
        )["spec"]["template"]["spec"]["containers"][0]["image"],
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
        raise ValueError("failed preflight candidate entered workflow state")
    candidate = expected_candidate(state)
    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("baseline route is not verified")
    route = json.loads(
        kube("kagent", "get", "virtualservice", "recsys-workflow-ab", "-o", "json")
    )
    if RELEASE in json.dumps(route["spec"]):
        raise ValueError("failed candidate unexpectedly appears in Istio routing")
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

    results = {}
    for probe in PROBES:
        key = "workflow/runtime-preflights/" + RELEASE + "/" + probe
        intent = json.loads(
            store.client.get_object(Bucket=store.bucket, Key=key + "/intent.json")[
                "Body"
            ].read()
        )
        result = json.loads(
            store.client.get_object(Bucket=store.bucket, Key=key + "/result.json")[
                "Body"
            ].read()
        )
        if (
            intent.get("inference_budget") != 1
            or result.get("verdict") != "FAIL"
            or result.get("reason") != "timeout_or_transport_failure_no_retry"
            or result.get("release_id") != RELEASE
            or result.get("probe") != probe
            or result.get("source") != "infrastructure_test"
            or result.get("offline_requests") != 0
            or result.get("synthetic_requests") != 0
        ):
            raise ValueError("exact failed create-only probe evidence required")
        results[probe] = {"intent": intent, "result": result}

    before = trajectory(candidate)
    if any(value["calls"] < 2 for value in before.values()):
        raise ValueError("duplicate candidate A2A loop evidence required")

    base = "rec-ab-" + RELEASE[:20]
    adapters = (
        _deployment(base, "ml-system"),
        _deployment(base + "-cpu", "cpu-services"),
    )
    coordinator = resource_name(members(candidate)["coordinator"])
    agent = json.loads(kube("kagent", "get", "sandboxagent", coordinator, "-o", "json"))
    model = json.loads(kube("kagent", "get", "modelconfig", coordinator, "-o", "json"))
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
        agent["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
        or model["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
        or len(templates) != 1
    ):
        raise ValueError("failed candidate coordinator ownership drift")
    backend = json.loads(
        kube(
            "kagent",
            "get",
            "deployment",
            "rec-llm-" + candidate["llm_version_id"][:20],
            "-o",
            "json",
        )
    )
    if backend["spec"].get("replicas") != 1 or backend.get("status", {}).get(
        "readyReplicas"
    ) != 1:
        raise ValueError("retained shared candidate backend is not Ready")

    journal = (
        "workflow/capacity-windows/failed-candidate-loop-quarantine-"
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
                "stage": "failed_candidate_loop_quarantine_intent",
                "build_url": os.environ["BUILD_URL"],
                "state_etag": etag,
                "baseline_release_id": BASELINE,
                "candidate": candidate,
                "probe_evidence": results,
                "trajectory_before": before,
                "adapters": adapters,
                "coordinator_agent": agent,
                "coordinator_model_config": model,
                "coordinator_actor_templates": templates,
                "retained_backend": backend,
                "counts": counts,
            },
            sort_keys=True,
        ).encode(),
        ContentType="application/json",
        IfNoneMatch="*",
    )

    selectors = [obj["spec"]["selector"]["matchLabels"] for obj in adapters]
    for obj in adapters:
        if obj["spec"].get("replicas") == 1:
            kube(
                "kagent",
                "patch",
                "deployment",
                obj["metadata"]["name"],
                "--type=json",
                "-p",
                json.dumps(
                    [
                        {
                            "op": "test",
                            "path": "/metadata/resourceVersion",
                            "value": obj["metadata"]["resourceVersion"],
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
            command("kubectl", "get", "pods", "-n", "kagent", "-o", "json")
        )["items"]
        admitted = [
            pod
            for pod in pods
            if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            and any(
                all(
                    pod["metadata"].get("labels", {}).get(key) == value
                    for key, value in selector.items()
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
            raise ValueError("HOLD failed candidate actor/adapters did not terminate")
        time.sleep(3)

    # TaskStore history is intentionally retained.  Absence of the owning
    # ActorTemplate must make its duplicate-call counters quiescent.
    first = trajectory(candidate)
    time.sleep(10)
    second = trajectory(candidate)
    if first != second:
        raise ValueError("failed candidate trajectory still changes after quarantine")
    backend_after = json.loads(
        kube(
            "kagent",
            "get",
            "deployment",
            "rec-llm-" + candidate["llm_version_id"][:20],
            "-o",
            "json",
        )
    )
    after, after_etag = store.read()
    if (
        backend_after["spec"].get("replicas") != 1
        or backend_after.get("status", {}).get("readyReplicas") != 1
        or after != state
        or after_etag != etag
        or not driver.verify_route(after, 0, after["route_revision"])
    ):
        raise ValueError("backend/state/route preservation failure")

    report = {
        "stage": "failed_candidate_loop_quarantine",
        "candidate_release_id": RELEASE,
        "journal_key": journal,
        "adapters_scaled_to_zero": [obj["metadata"]["name"] for obj in adapters],
        "coordinator_sandboxagent_removed": coordinator,
        "coordinator_modelconfig_retained": coordinator,
        "specialist_agents_retained": True,
        "llm_backend_retained_ready": True,
        "taskstore_and_minio_evidence_retained": True,
        "trajectory_quiescent": second,
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
