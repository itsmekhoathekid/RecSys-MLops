"""Release adapters for one exact failed, unactivated v5 baseline."""
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

REVIEW = "reviewed-failed-v5-baseline-adapters-v1"
RELEASE = "d579ee0a004dc0b4125d84304b1bc4d41b0967fc03b8f26bdfe49f9d09c3682b"
TARGETS = {
    "rec-ab-d579ee0a004dc0b4125d": "ml-system",
    "rec-ab-d579ee0a004dc0b4125d-cpu": "cpu-services",
}


def main():
    p = argparse.ArgumentParser(); p.add_argument("--image", required=True)
    if p.parse_args().image != REVIEW:
        raise ValueError("unreviewed failed-baseline capacity action")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")
    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = json.loads(kube("kagent", "get", "deployment", "recsys-workflow-router", "-o", "json"))
    os.environ.update(AB_SCOPE="workflow", AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0]["image"])
    check(); store = StateStore("s3://recsys-llm-ab/workflow/state.json"); state, etag = store.read(); driver = Driver()
    referenced = {state.get(key, {}).get("release_id") for key in ("champion", "previous", "baseline", "pending")}
    referenced.update(state.get("releases", {}).keys())
    if state.get("phase") != "ROLLED_BACK" or RELEASE in referenced:
        raise ValueError("failed baseline unexpectedly active or retained in state")
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("rollback route is not verified")
    probe_root = "workflow/runtime-preflights/" + RELEASE + "/recommendation-serving-v1/"
    intent_key, result_key = probe_root + "intent.json", probe_root + "result.json"
    intent = json.loads(store.client.get_object(Bucket=store.bucket, Key=intent_key)["Body"].read())
    if intent.get("source") != "infrastructure_test" or intent.get("inference_budget") != 1:
        raise ValueError("exact failed serving preflight intent required")
    from botocore.exceptions import ClientError
    try:
        store.client.get_object(Bucket=store.bucket, Key=result_key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
            raise
    else:
        raise ValueError("reviewed v5 failure must remain an unresolved timeout intent")
    deployments = []
    for name, pool in TARGETS.items():
        obj = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
        env = [entry for container in obj["spec"]["template"]["spec"]["containers"]
               for entry in container.get("env", []) if entry["name"] == "RELEASE_ID"]
        if (obj["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
                or obj["spec"].get("replicas") not in {0, 1}
                or obj["spec"]["template"]["spec"].get("nodeSelector") != {"recsys.ai/pool": pool}
                or env != [{"name": "RELEASE_ID", "value": RELEASE}]):
            raise ValueError("failed baseline adapter identity drift: " + name)
        if obj["spec"]["replicas"] and obj["status"].get("readyReplicas") != 1:
            raise ValueError("failed baseline adapter not Ready before scale-down")
        deployments.append(obj)
    with driver.db.connect() as c:
        sessions = c.execute("SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s", (RELEASE,)).fetchone()["n"]
        unfinished = c.execute("SELECT count(*) n FROM recsys_ab.invocations WHERE release_id=%s AND finished_at IS NULL", (RELEASE,)).fetchone()["n"]
    if sessions or unfinished:
        raise ValueError("failed baseline has session or unfinished invocation")
    nodes = json.loads(command("kubectl", "get", "nodes", "-o", "json"))["items"]
    pods = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    selectors = [obj["spec"]["selector"]["matchLabels"] for obj in deployments]
    def target(pod):
        return pod["metadata"]["namespace"] == "kagent" and pod["status"]["phase"] not in {"Succeeded", "Failed"} and any(
            all(pod["metadata"].get("labels", {}).get(k) == v for k, v in selector.items()) for selector in selectors)
    planned = [pod for pod in pods if not target(pod)]
    reservations = workflow_job_reservations(planned, probe=True) + native_reservations(include_backend=False)
    projected = verify_capacity(nodes, planned, reservations, {}, headroom={"cpu": "200m", "memory": "128Mi"})
    key = "workflow/capacity-windows/failed-v5-baseline-" + os.environ["BUILD_NUMBER"] + ".json"
    store.client.put_object(Bucket=store.bucket, Key=key, Body=json.dumps({"stage": "failed_baseline_adapter_release",
        "build_url": os.environ["BUILD_URL"], "state_etag": etag, "targets": deployments,
        "failed_preflight_intent": intent_key, "failure":"jenkins-25-read-timeout-no-retry",
        "sessions": sessions, "unfinished": unfinished,
        "projected_headroom": {n: {k: str(v) for k, v in x.items()} for n, x in projected.items()}}).encode(),
        ContentType="application/json", IfNoneMatch="*")
    for obj in deployments:
        if obj["spec"]["replicas"]:
            kube("kagent", "patch", "deployment", obj["metadata"]["name"], "--type=json", "-p", json.dumps([
                {"op": "test", "path": "/metadata/resourceVersion", "value": obj["metadata"]["resourceVersion"]},
                {"op": "test", "path": "/spec/replicas", "value": 1},
                {"op": "replace", "path": "/spec/replicas", "value": 0}]))
    deadline = time.monotonic() + 180
    while any(target(pod) for pod in json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]):
        if time.monotonic() > deadline:
            raise ValueError("HOLD graceful failed-baseline scale-down; no force delete")
        time.sleep(3)
    current = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    final = verify_capacity(nodes, current,
        workflow_job_reservations(current, probe=True) + native_reservations(include_backend=False), {},
        headroom={"cpu": "200m", "memory": "128Mi"})
    after, after_etag = store.read()
    if after != state or after_etag != etag or not driver.verify_route(after, 0, after["route_revision"]):
        raise ValueError("route/state preservation failure")
    report = {"stage": "failed_baseline_adapter_release", "snapshot_key": key, "target_replicas": 0,
        "release_backend_and_failed_evidence_retained": True, "state_unchanged": True, "route_unchanged": True,
        "replacement_adapter_pair_capacity_verified": True,
        "projected_headroom": {n: {k: str(v) for k, v in x.items()} for n, x in final.items()}}
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))

if __name__ == "__main__": main()
