"""Release only adapter capacity for the current rolled-back candidate.

The immutable release, model backend and evidence remain retained. Both adapter
placements may stop only because the candidate has no session or invocation.
"""
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


REVIEW = "reviewed-current-rolled-back-candidate-adapters-v1"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    if parser.parse_args().image != REVIEW:
        raise ValueError("unreviewed failed-candidate capacity action")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")
    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = json.loads(kube("kagent", "get", "deployment", "recsys-workflow-router", "-o", "json"))
    os.environ.update(AB_SCOPE="workflow", AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0]["image"])
    check()
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    release_id = state.get("pending", {}).get("release_id")
    champion_id = state.get("champion", {}).get("release_id")
    if (state.get("phase") != "ROLLED_BACK" or not release_id or not champion_id
            or release_id == champion_id or release_id not in state.get("disabled", [])):
        raise ValueError("exact current rolled-back candidate state required")
    targets = {
        "rec-ab-" + release_id[:20]: "ml-system",
        "rec-ab-" + release_id[:20] + "-cpu": "cpu-services",
    }
    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("rollback route is not verified")

    deployments = []
    for name, pool in targets.items():
        obj = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
        env = [entry for container in obj["spec"]["template"]["spec"]["containers"]
               for entry in container.get("env", []) if entry["name"] == "RELEASE_ID"]
        if (obj["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
                or obj["spec"].get("replicas") not in {0, 1}
                or obj["spec"]["template"]["spec"].get("nodeSelector") != {"recsys.ai/pool": pool}
                or env != [{"name": "RELEASE_ID", "value": release_id}]):
            raise ValueError("failed candidate adapter identity drift: " + name)
        if obj["spec"]["replicas"] == 1 and obj["status"].get("readyReplicas") != 1:
            raise ValueError("failed candidate adapter is not Ready before scale-down")
        deployments.append(obj)
    with driver.db.connect() as connection:
        sessions = connection.execute(
            "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s", (release_id,)
        ).fetchone()["n"]
        unfinished = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations WHERE release_id=%s AND finished_at IS NULL", (release_id,)
        ).fetchone()["n"]
    if sessions or unfinished:
        raise ValueError("failed candidate still has a session or unfinished invocation")

    nodes = json.loads(command("kubectl", "get", "nodes", "-o", "json"))["items"]
    pods = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    selectors = [obj["spec"]["selector"]["matchLabels"] for obj in deployments]
    def target_pod(pod):
        return (pod["metadata"]["namespace"] == "kagent"
                and pod["status"]["phase"] not in {"Succeeded", "Failed"}
                and any(all(pod["metadata"].get("labels", {}).get(k) == v for k, v in selector.items())
                        for selector in selectors))
    planned = [pod for pod in pods if not target_pod(pod)]
    reserve = workflow_job_reservations(planned, probe=True) + native_reservations(include_backend=False)
    projected = verify_capacity(nodes, planned, reserve, {}, headroom={"cpu": "200m", "memory": "128Mi"})
    key = ("workflow/capacity-windows/rolled-back-candidate-" + release_id[:20]
           + "-" + os.environ["BUILD_NUMBER"] + ".json")
    snapshot = {"stage": "failed_candidate_adapter_release", "build_url": os.environ["BUILD_URL"],
        "state_etag": etag, "champion_release_id": champion_id,
        "candidate_release_id": release_id, "targets": deployments,
        "sessions": sessions, "unfinished": unfinished,
        "projected_headroom": {n: {k: str(v) for k, v in values.items()} for n, values in projected.items()}}
    store.client.put_object(Bucket=store.bucket, Key=key, Body=json.dumps(snapshot).encode(),
        ContentType="application/json", IfNoneMatch="*")
    for obj in deployments:
        if obj["spec"]["replicas"] == 1:
            kube("kagent", "patch", "deployment", obj["metadata"]["name"], "--type=json", "-p", json.dumps([
                {"op": "test", "path": "/metadata/resourceVersion", "value": obj["metadata"]["resourceVersion"]},
                {"op": "test", "path": "/spec/replicas", "value": 1},
                {"op": "replace", "path": "/spec/replicas", "value": 0},
            ]))
    deadline = time.monotonic() + 180
    while any(target_pod(pod) for pod in json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]):
        if time.monotonic() > deadline:
            raise ValueError("HOLD graceful adapter scale-down; no force delete")
        time.sleep(3)
    current = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    final = verify_capacity(nodes, current,
        workflow_job_reservations(current, probe=True) + native_reservations(include_backend=False), {},
        headroom={"cpu": "200m", "memory": "128Mi"})
    after, after_etag = store.read()
    if after != state or after_etag != etag or not driver.verify_route(after, 0, after["route_revision"]):
        raise ValueError("route/state preservation failure")
    report = {"stage": "failed_candidate_adapter_release", "snapshot_key": key,
        "target_replicas": 0, "release_and_backend_retained": True, "state_unchanged": True,
        "route_unchanged": True, "replacement_adapter_pair_capacity_verified": True,
        "projected_headroom": {n: {k: str(v) for k, v in values.items()} for n, values in final.items()}}
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
