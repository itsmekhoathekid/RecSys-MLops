"""Reduce only duplicate adapter replicas while retaining every serving release."""
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


REVIEW = "reviewed-active-history-rightsize-v11"
CHAMPION = "8a62a3d15896940c535aab556e83e6ce00c1c5b98716f6ef079e93a245a425c6"
TARGETS = {
    "rec-ab-7c965ae4a090d308fb5d": "7c965ae4a090d308fb5d030a97c00e5d6af55a162fc8f94248e7ad7295732f4c",
    "rec-ab-e10b72a49e62723eaab3": "e10b72a49e62723eaab393eadd7775cd4724bbcf4fd29028a1e93ce8fd8146a4",
    "rec-ab-924e9b8b84915b6fd2e6": "924e9b8b84915b6fd2e68e61309481f5de7c9aa173de7c5dae2096ac6fdcc8c1",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if args.image != REVIEW:
        raise ValueError("unreviewed active capacity window")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")
    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    os.environ.update(AB_SCOPE="workflow", AB_ROUTER_IMAGE=json.loads(
        kube("kagent", "get", "deployment", "recsys-workflow-router", "-o", "json")
    )["spec"]["template"]["spec"]["containers"][0]["image"])
    check()
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    driver = Driver()
    if (state["phase"] != "IDLE" or not state.get("activated") or state.get("experiment_id")
            or state["champion"]["release_id"] != CHAMPION):
        raise ValueError("exact idle activated workflow champion required")
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("baseline route is not verified")

    deployments = []
    session_counts = {}
    with driver.db.connect() as connection:
        for name, release_id in TARGETS.items():
            obj = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
            env = [entry for container in obj["spec"]["template"]["spec"]["containers"]
                   for entry in container.get("env", []) if entry["name"] == "RELEASE_ID"]
            if (obj["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
                    or obj["spec"]["replicas"] not in {1, 2}
                    or obj["status"].get("readyReplicas", 0) != obj["spec"]["replicas"]
                    or env != [{"name": "RELEASE_ID", "value": release_id}]):
                raise ValueError("historical adapter identity/readiness drift: " + name)
            unfinished = connection.execute(
                "SELECT count(*) AS n FROM recsys_ab.invocations WHERE release_id=%s AND finished_at IS NULL",
                (release_id,),
            ).fetchone()["n"]
            if unfinished:
                raise ValueError("historical adapter has unfinished invocation: " + name)
            session_counts[release_id] = connection.execute(
                "SELECT count(*) AS n FROM recsys_ab.sessions WHERE release_id=%s", (release_id,)
            ).fetchone()["n"]
            deployments.append(obj)

    nodes = json.loads(command("kubectl", "get", "nodes", "-o", "json"))["items"]
    pods = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    excluded = set()
    for obj in deployments:
        if obj["spec"]["replicas"] == 2:
            selector = obj["spec"]["selector"]["matchLabels"]
            matching = sorted(p["metadata"]["uid"] for p in pods
                if p["metadata"]["namespace"] == "kagent"
                and p["status"]["phase"] not in {"Succeeded", "Failed"}
                and all(p["metadata"].get("labels", {}).get(k) == v for k, v in selector.items()))
            if len(matching) != 2:
                raise ValueError("expected two admitted historical adapter pods")
            excluded.add(matching[-1])
    planned = [pod for pod in pods if pod["metadata"].get("uid") not in excluded]
    reservations = workflow_job_reservations(planned, probe=True)
    reservations.extend(native_reservations(include_backend=False))
    projected = verify_capacity(nodes, planned, reservations, {}, headroom={"cpu": "200m", "memory": "128Mi"})

    key = "workflow/capacity-windows/active-" + os.environ["BUILD_NUMBER"] + ".json"
    snapshot = {"stage": "active_history_rightsize", "build_url": os.environ["BUILD_URL"],
        "state_etag": etag, "targets": deployments, "session_counts": session_counts,
        "projected_headroom": {n: {k: str(v) for k, v in values.items()} for n, values in projected.items()}}
    store.client.put_object(Bucket=store.bucket, Key=key, Body=json.dumps(snapshot).encode(),
        ContentType="application/json", IfNoneMatch="*")
    for obj in deployments:
        if obj["spec"]["replicas"] == 2:
            kube("kagent", "patch", "deployment", obj["metadata"]["name"], "--type=json", "-p", json.dumps([
                {"op": "test", "path": "/metadata/resourceVersion", "value": obj["metadata"]["resourceVersion"]},
                {"op": "test", "path": "/spec/replicas", "value": 2},
                {"op": "replace", "path": "/spec/replicas", "value": 1},
            ]))
            kube("kagent", "rollout", "status", "deployment/" + obj["metadata"]["name"], "--timeout=180s")
    deadline = time.monotonic() + 180
    while True:
        current = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
        counts = []
        for obj in deployments:
            selector = obj["spec"]["selector"]["matchLabels"]
            counts.append(sum(p["metadata"]["namespace"] == "kagent"
                and p["status"]["phase"] not in {"Succeeded", "Failed"}
                and all(p["metadata"].get("labels", {}).get(k) == v for k, v in selector.items()) for p in current))
        if counts == [1] * len(deployments):
            break
        if time.monotonic() > deadline:
            raise ValueError("HOLD graceful adapter scale-down; no force delete")
        time.sleep(3)
    final_reservations = workflow_job_reservations(current, probe=True)
    final_reservations.extend(native_reservations(include_backend=False))
    final = verify_capacity(nodes, current, final_reservations, {}, headroom={"cpu": "200m", "memory": "128Mi"})
    after, after_etag = store.read()
    if after != state or after_etag != etag or not driver.verify_route(after, 0, after["route_revision"]):
        raise ValueError("route/state preservation failure")
    report = {"stage": "active_history_rightsize", "snapshot_key": key,
        "replicas_per_retained_release": 1, "sessions_retained": session_counts,
        "state_unchanged": True, "route_unchanged": True,
        "full_experiment_capacity_verified": True,
        "projected_headroom": {n: {k: str(v) for k, v in values.items()} for n, values in final.items()}}
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
