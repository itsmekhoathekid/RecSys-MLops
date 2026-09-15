"""Release adapter capacity for one failed, never-routed A2A preflight.

The candidate release, model backend, agents, TaskStore trajectory, and MinIO
intent remain retained.  Only the two stateless adapter Deployments scale to
zero after their aborted request is proven quiescent and absent from routing.
"""
import argparse
import json
import os
from pathlib import Path
import time
import uuid

from botocore.exceptions import ClientError

from apps.agentic.llm_ab_router.child_tasks import ChildTasks
from .capacity import verify_capacity, workflow_job_reservations
from .capacity_window import native_reservations
from .driver import command, Driver
from .provision import kube
from .release_guard import check
from .state import StateStore
from .workflow_evidence import events
from .evidence import task_of


REVIEW = "reviewed-failed-unrouted-a2a-ab4e-v1"
RELEASE = "ab4e20594c7e1680a2428fa954b26e09abf03ebad2e71b19904b9d9d35eaba13"
BASELINE = "25a9165703c266d5c363d5d1a8f4d41c057eea16b93f2e8c10cdcfbb085ec211"
PROBE = "candidate-coordinator-recommendation-a2a-v23"


def _trajectory():
    root = "workflow/runtime-preflights/" + RELEASE + "/" + PROBE
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, root))
    reader = ChildTasks(
        "workflow-infrastructure-preflight",
        ["kagent__NS__rec_ab_" + RELEASE[:20]],
    )
    try:
        body = reader(request_id)
    finally:
        reader.close()
    task = task_of(body)
    return {
        "request_id": request_id,
        "state": task.get("status", {}).get("state"),
        "calls": len(events(task, "function_call")),
        "responses": len(events(task, "function_response")),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    if parser.parse_args().image != REVIEW:
        raise ValueError("unreviewed failed preflight capacity action")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")
    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    os.environ.update(
        AB_SCOPE="workflow",
        AB_KAGENT_GRPC_TARGET="kagent-controller.kagent.svc.cluster.local:8084",
        AB_ROUTER_IMAGE=json.loads(kube(
            "kagent", "get", "deployment", "recsys-workflow-router", "-o", "json"
        ))["spec"]["template"]["spec"]["containers"][0]["image"],
    )
    check("workflow")
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    if (state.get("phase") != "IDLE" or not state.get("activated")
            or state.get("experiment_id")
            or state.get("champion", {}).get("release_id") != BASELINE):
        raise ValueError("exact post-preflight IDLE baseline required")
    protected = {(state.get(k) or {}).get("release_id") for k in
                 ("champion", "previous", "baseline", "pending")}
    if RELEASE in protected or RELEASE in state.get("releases", {}):
        raise ValueError("failed preflight release unexpectedly entered release state")
    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("baseline route is not verified")
    route = json.loads(kube(
        "kagent", "get", "virtualservice", "recsys-workflow-ab", "-o", "json"))
    if RELEASE in json.dumps(route["spec"]):
        raise ValueError("failed preflight candidate is routable")

    intent_key = ("workflow/runtime-preflights/" + RELEASE + "/" + PROBE
                  + "/intent.json")
    result_key = ("workflow/runtime-preflights/" + RELEASE + "/" + PROBE
                  + "/result.json")
    intent = json.loads(store.client.get_object(
        Bucket=store.bucket, Key=intent_key)["Body"].read())
    try:
        terminal = json.loads(store.client.get_object(
            Bucket=store.bucket, Key=result_key)["Body"].read())
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
            raise
        terminal = None
    if terminal is not None and (
            terminal.get("source") != "infrastructure_test"
            or terminal.get("role") != "coordinator"
            or terminal.get("release_id") != RELEASE
            or terminal.get("probe") != PROBE
            or terminal.get("verdict") != "FAIL"
            or terminal.get("offline_requests") != 0
            or terminal.get("synthetic_requests") != 0):
        raise ValueError("exact terminal failed-preflight evidence required")
    first = _trajectory()
    time.sleep(5)
    second = _trajectory()
    if (first != second or first["calls"] < 2
            or first["calls"] != first["responses"]):
        raise ValueError("failed preflight trajectory is not quiescent")

    targets = []
    for name, pool in (("rec-ab-" + RELEASE[:20], "ml-system"),
                       ("rec-ab-" + RELEASE[:20] + "-cpu", "cpu-services")):
        obj = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
        release_env = [entry for c in obj["spec"]["template"]["spec"]["containers"]
                       for entry in c.get("env", []) if entry["name"] == "RELEASE_ID"]
        if (obj["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
                or obj["spec"].get("replicas") != 1
                or obj["status"].get("readyReplicas") != 1
                or obj["spec"]["template"]["spec"].get("nodeSelector")
                    != {"recsys.ai/pool": pool}
                or release_env != [{"name": "RELEASE_ID", "value": RELEASE}]):
            raise ValueError("failed preflight adapter identity/readiness drift")
        targets.append(obj)
    with driver.db.connect() as connection:
        sessions = connection.execute(
            "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s",
            (RELEASE,)).fetchone()["n"]
        invocations = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations WHERE release_id=%s",
            (RELEASE,)).fetchone()["n"]
    if sessions or invocations:
        raise ValueError("unrouted preflight candidate entered routing database")

    nodes = json.loads(command("kubectl", "get", "nodes", "-o", "json"))["items"]
    pods = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    selectors = [obj["spec"]["selector"]["matchLabels"] for obj in targets]
    def target(pod):
        return (pod["metadata"].get("namespace") == "kagent"
                and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
                and any(all(pod["metadata"].get("labels", {}).get(k) == v
                            for k, v in selector.items()) for selector in selectors))
    planned = [pod for pod in pods if not target(pod)]
    projected = verify_capacity(
        nodes, planned,
        workflow_job_reservations(planned, probe=True)
        + native_reservations(include_backend=False), {},
        headroom={"cpu": "200m", "memory": "128Mi"})
    journal = ("workflow/capacity-windows/failed-unrouted-a2a-"
               + RELEASE[:20] + "-" + os.environ["BUILD_NUMBER"] + ".json")
    store.client.put_object(Bucket=store.bucket, Key=journal,
        Body=json.dumps({"stage": "failed_unrouted_a2a_adapter_release",
            "build_url": os.environ["BUILD_URL"], "state_etag": etag,
            "baseline_release_id": BASELINE, "candidate_release_id": RELEASE,
            "intent_key": intent_key, "intent": intent,
            "terminal_result_key": result_key if terminal else None,
            "terminal_result": terminal,
            "quiescent_trajectory": second, "targets": targets,
            "sessions": sessions, "invocations": invocations,
            "projected_headroom": {n: {k: str(v) for k, v in values.items()}
                                   for n, values in projected.items()}}).encode(),
        ContentType="application/json", IfNoneMatch="*")
    for obj in targets:
        kube("kagent", "patch", "deployment", obj["metadata"]["name"],
             "--type=json", "-p", json.dumps([
                {"op": "test", "path": "/metadata/resourceVersion",
                 "value": obj["metadata"]["resourceVersion"]},
                {"op": "test", "path": "/spec/replicas", "value": 1},
                {"op": "replace", "path": "/spec/replicas", "value": 0},
             ]))
    deadline = time.monotonic() + 180
    while any(target(p) for p in json.loads(command(
            "kubectl", "get", "pods", "-A", "-o", "json"))["items"]):
        if time.monotonic() >= deadline:
            raise ValueError("HOLD graceful failed-preflight adapter scale-down")
        time.sleep(3)
    after, after_etag = store.read()
    if (after != state or after_etag != etag
            or not driver.verify_route(after, 0, after["route_revision"])):
        raise ValueError("state or route changed during adapter release")
    report = {"stage": "failed_unrouted_a2a_adapter_release",
        "candidate_release_id": RELEASE, "targets_scaled_to_zero":
        [obj["metadata"]["name"] for obj in targets],
        "quiescent_duplicate_calls": second["calls"],
        "release_backend_agents_and_evidence_retained": True,
        "state_unchanged": True, "route_unchanged": True,
        "journal_key": journal}
    target_file = Path(".llm-agent-cd/evaluation-preparation.json")
    target_file.parent.mkdir(exist_ok=True)
    target_file.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
