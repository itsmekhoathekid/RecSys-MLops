"""Retain a disabled candidate on ML while releasing its duplicate CPU adapter."""
import argparse
import json
import os
from pathlib import Path
import time

from .capacity import verify_capacity, workflow_job_reservations
from .capacity_window import native_reservations
from .driver import command, Driver
from .provision import kube
from .release_guard import check
from .state import StateStore

REVIEW = "reviewed-disabled-candidate-single-placement-v1"
CHAMPION = "8a62a3d15896940c535aab556e83e6ce00c1c5b98716f6ef079e93a245a425c6"
RELEASE = "b0c946863001e0aa5d5c5b8e087e953863eb22c5a8a84f536f4eb89ca3f5e445"
TARGET = "rec-ab-b0c946863001e0aa5d5c-cpu"
PEER = "rec-ab-b0c946863001e0aa5d5c"
SERVICE = "rec-ab-b0c946863001e0aa5d5c"


def validate_state(state):
    if (state.get("phase") != "ROLLED_BACK" or state.get("champion", {}).get("release_id") != CHAMPION
            or state.get("pending", {}).get("release_id") != RELEASE
            or RELEASE not in state.get("disabled", [])):
        raise ValueError("exact disabled-candidate rollback state required")


def deployment(name, pool):
    obj = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
    env = [entry for container in obj["spec"]["template"]["spec"]["containers"]
           for entry in container.get("env", []) if entry["name"] == "RELEASE_ID"]
    if (obj["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
            or obj["spec"].get("replicas") not in {0, 1}
            or env != [{"name": "RELEASE_ID", "value": RELEASE}]
            or obj["spec"]["template"]["spec"].get("nodeSelector") != {"recsys.ai/pool": pool}):
        raise ValueError("candidate adapter identity drift: " + name)
    return obj


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if args.image != REVIEW:
        raise ValueError("unreviewed adapter placement capacity action")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")
    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = json.loads(kube("kagent", "get", "deployment", "recsys-workflow-router", "-o", "json"))
    os.environ.update(AB_SCOPE="workflow", AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0]["image"])
    check()
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    validate_state(state)
    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("rollback route is not verified")
    target = deployment(TARGET, "cpu-services")
    peer = deployment(PEER, "ml-system")
    if peer["spec"]["replicas"] != 1 or peer["status"].get("readyReplicas") != 1:
        raise ValueError("retained ML candidate adapter is not Ready")
    with driver.db.connect() as connection:
        unfinished = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations WHERE release_id=%s AND finished_at IS NULL", (RELEASE,)
        ).fetchone()["n"]
        sessions = connection.execute(
            "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s", (RELEASE,)
        ).fetchone()["n"]
    if unfinished:
        raise ValueError("candidate has unfinished invocation")
    nodes = json.loads(command("kubectl", "get", "nodes", "-o", "json"))["items"]
    pods = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    selector = target["spec"]["selector"]["matchLabels"]
    def target_pod(p):
        return (p["metadata"]["namespace"] == "kagent" and p["status"]["phase"] not in {"Succeeded", "Failed"}
                and all(p["metadata"].get("labels", {}).get(k) == v for k, v in selector.items()))
    planned = [p for p in pods if not target_pod(p)]
    reserve = workflow_job_reservations(planned, probe=True) + native_reservations(include_backend=False)
    projected = verify_capacity(nodes, planned, reserve, {}, headroom={"cpu": "200m", "memory": "128Mi"})
    key = "workflow/capacity-windows/disabled-candidate-placement-" + os.environ["BUILD_NUMBER"] + ".json"
    snapshot = {"stage": "disabled_candidate_single_placement", "build_url": os.environ["BUILD_URL"],
        "state_etag": etag, "target": target, "retained_peer": peer, "sessions_retained": sessions,
        "projected_headroom": {n: {k: str(v) for k, v in values.items()} for n, values in projected.items()}}
    store.client.put_object(Bucket=store.bucket, Key=key, Body=json.dumps(snapshot).encode(),
        ContentType="application/json", IfNoneMatch="*")
    if target["spec"]["replicas"] == 1:
        kube("kagent", "patch", "deployment", TARGET, "--type=json", "-p", json.dumps([
            {"op": "test", "path": "/metadata/resourceVersion", "value": target["metadata"]["resourceVersion"]},
            {"op": "test", "path": "/spec/replicas", "value": 1},
            {"op": "replace", "path": "/spec/replicas", "value": 0},
        ]))
    deadline = time.monotonic() + 180
    while any(target_pod(p) for p in json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]):
        if time.monotonic() > deadline:
            raise ValueError("HOLD graceful adapter scale-down; no force delete")
        time.sleep(3)
    peer = deployment(PEER, "ml-system")
    if peer["spec"]["replicas"] != 1 or peer["status"].get("readyReplicas") != 1:
        raise ValueError("retained candidate adapter lost readiness")
    slices = json.loads(kube("kagent", "get", "endpointslice", "-l", "kubernetes.io/service-name=" + SERVICE, "-o", "json"))["items"]
    ready = sum(1 for item in slices for endpoint in item.get("endpoints", []) if endpoint.get("conditions", {}).get("ready") is True)
    if ready < 1:
        raise ValueError("retained candidate Service has no Ready endpoint")
    current = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    final_reserve = workflow_job_reservations(current, probe=True) + native_reservations(include_backend=False)
    final = verify_capacity(nodes, current, final_reserve, {}, headroom={"cpu": "200m", "memory": "128Mi"})
    after, after_etag = store.read()
    if after != state or after_etag != etag or not driver.verify_route(after, 0, after["route_revision"]):
        raise ValueError("route/state preservation failure")
    report = {"stage": "disabled_candidate_single_placement", "snapshot_key": key,
        "target_replicas": 0, "retained_peer_replicas": 1, "sessions_retained": sessions,
        "state_unchanged": True, "route_unchanged": True, "baseline_capacity_verified": True,
        "projected_headroom": {n: {k: str(v) for k, v in values.items()} for n, values in final.items()}}
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
