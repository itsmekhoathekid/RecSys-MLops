"""Retain one endpoint per serving release while reserving a new A/B adapter pair."""
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

REVIEW = "reviewed-serving-single-placement-and-router-v3"
GATEWAY = "recsys-workflow-gateway"
ROUTER = "recsys-workflow-router"
CHAMPION = "debe3d60e89ecdb495f12f0f6b20d409b254b93e3479da40b32d34c3163a074d"
RELEASE = "8a62a3d15896940c535aab556e83e6ce00c1c5b98716f6ef079e93a245a425c6"
TARGETS = (
    {"release": RELEASE, "target": "rec-ab-8a62a3d15896940c535a-cpu", "target_pool": "cpu-services",
     "peer": "rec-ab-8a62a3d15896940c535a", "peer_pool": "ml-system", "service": "rec-ab-8a62a3d15896940c535a"},
    {"release": CHAMPION, "target": "rec-ab-debe3d60e89ecdb495f1", "target_pool": "ml-system",
     "peer": "rec-ab-debe3d60e89ecdb495f1-cpu", "peer_pool": "cpu-services", "service": "rec-ab-debe3d60e89ecdb495f1"},
)


def validate_state(state):
    if (state.get("phase") != "IDLE" or not state.get("activated") or state.get("experiment_id")
            or state.get("champion", {}).get("release_id") != CHAMPION
            or state.get("previous", {}).get("release_id") != RELEASE):
        raise ValueError("exact idle new-baseline/previous-champion state required")


def deployment(name, pool, release):
    obj = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
    env = [entry for container in obj["spec"]["template"]["spec"]["containers"]
           for entry in container.get("env", []) if entry["name"] == "RELEASE_ID"]
    if (obj["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
            or obj["spec"].get("replicas") not in {0, 1}
            or env != [{"name": "RELEASE_ID", "value": release}]
            or obj["spec"]["template"]["spec"].get("nodeSelector") != {"recsys.ai/pool": pool}):
        raise ValueError("previous adapter identity drift: " + name)
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
    records = []
    for spec in TARGETS:
        target = deployment(spec["target"], spec["target_pool"], spec["release"])
        peer = deployment(spec["peer"], spec["peer_pool"], spec["release"])
        if peer["spec"]["replicas"] != 1 or peer["status"].get("readyReplicas") != 1:
            raise ValueError("retained adapter peer is not Ready")
        records.append({**spec, "target_obj": target, "peer_obj": peer})
    gateway = json.loads(kube("kagent", "get", "deployment", GATEWAY, "-o", "json"))
    if (gateway["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != "Helm"
            or gateway["spec"]["selector"].get("matchLabels") != {"istio": GATEWAY}
            or gateway["spec"].get("replicas") not in {1, 2}
            or gateway["status"].get("readyReplicas") != gateway["spec"].get("replicas")):
        raise ValueError("workflow gateway identity/readiness drift")
    router_deployment = json.loads(kube("kagent", "get", "deployment", ROUTER, "-o", "json"))
    if (router_deployment["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != "Helm"
            or router_deployment["spec"]["selector"].get("matchLabels") != {"app": ROUTER}
            or router_deployment["spec"].get("replicas") not in {1, 2}
            or router_deployment["status"].get("readyReplicas") != router_deployment["spec"].get("replicas")):
        raise ValueError("workflow router identity/readiness drift")
    # Route attestation requires two Envoy gateways. Restore that invariant
    # before validating the data plane; the capacity exchange is router 2->1.
    if gateway["spec"]["replicas"] == 1:
        kube("kagent", "patch", "deployment", GATEWAY, "--type=json", "-p", json.dumps([
            {"op": "test", "path": "/metadata/resourceVersion", "value": gateway["metadata"]["resourceVersion"]},
            {"op": "test", "path": "/spec/replicas", "value": 1},
            {"op": "replace", "path": "/spec/replicas", "value": 2},
        ]))
        kube("kagent", "rollout", "status", "deployment/" + GATEWAY, "--timeout=180s")
        gateway = json.loads(kube("kagent", "get", "deployment", GATEWAY, "-o", "json"))
    if gateway["spec"]["replicas"] != 2 or gateway["status"].get("readyReplicas") != 2:
        raise ValueError("two Ready Envoy gateways required")
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("baseline route is not verified")
    sessions = {}
    with driver.db.connect() as connection:
        for spec in TARGETS:
            unfinished = connection.execute(
                "SELECT count(*) n FROM recsys_ab.invocations WHERE release_id=%s AND finished_at IS NULL", (spec["release"],)
            ).fetchone()["n"]
            sessions[spec["release"]] = connection.execute(
                "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s", (spec["release"],)
            ).fetchone()["n"]
            if unfinished:
                raise ValueError("serving release has unfinished invocation")
    nodes = json.loads(command("kubectl", "get", "nodes", "-o", "json"))["items"]
    pods = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    selectors = [record["target_obj"]["spec"]["selector"]["matchLabels"] for record in records]
    router_selector = router_deployment["spec"]["selector"]["matchLabels"]
    def target_pod(p):
        return (p["metadata"]["namespace"] == "kagent" and p["status"]["phase"] not in {"Succeeded", "Failed"}
                and any(all(p["metadata"].get("labels", {}).get(k) == v for k, v in selector.items())
                        for selector in selectors))
    router_pods = sorted((p for p in pods if p["metadata"]["namespace"] == "kagent"
        and p["status"]["phase"] not in {"Succeeded", "Failed"}
        and all(p["metadata"].get("labels", {}).get(k) == v for k, v in router_selector.items())),
        key=lambda p: p["metadata"]["uid"])
    if len(router_pods) != router_deployment["spec"]["replicas"]:
        raise ValueError("workflow router admitted pod count drift")
    removed_router_uid = router_pods[-1]["metadata"]["uid"] if len(router_pods) == 2 else None
    planned = [p for p in pods if not target_pod(p) and p["metadata"].get("uid") != removed_router_uid]
    # One pair is the new candidate; the second is the currently paused
    # champion placement that Workflow-CD will restore from its immutable spec.
    pair = native_reservations(include_backend=False)
    reserve = workflow_job_reservations(planned, probe=True)
    reserve += pair + [pair[0]]
    projected = verify_capacity(nodes, planned, reserve, {}, headroom={"cpu": "200m", "memory": "128Mi"})
    key = "workflow/capacity-windows/previous-champion-placement-" + os.environ["BUILD_NUMBER"] + ".json"
    snapshot = {"stage": "serving_releases_single_placement", "build_url": os.environ["BUILD_URL"],
        "state_etag": etag, "targets": [r["target_obj"] for r in records],
        "retained_peers": [r["peer_obj"] for r in records], "gateway": gateway,
        "router": router_deployment,
        "sessions_retained": sessions,
        "projected_headroom": {n: {k: str(v) for k, v in values.items()} for n, values in projected.items()}}
    store.client.put_object(Bucket=store.bucket, Key=key, Body=json.dumps(snapshot).encode(),
        ContentType="application/json", IfNoneMatch="*")
    for record in records:
        target = record["target_obj"]
        if target["spec"]["replicas"] == 1:
            kube("kagent", "patch", "deployment", target["metadata"]["name"], "--type=json", "-p", json.dumps([
                {"op": "test", "path": "/metadata/resourceVersion", "value": target["metadata"]["resourceVersion"]},
                {"op": "test", "path": "/spec/replicas", "value": 1},
                {"op": "replace", "path": "/spec/replicas", "value": 0},
            ]))
    if router_deployment["spec"]["replicas"] == 2:
        kube("kagent", "patch", "deployment", ROUTER, "--type=json", "-p", json.dumps([
            {"op": "test", "path": "/metadata/resourceVersion", "value": router_deployment["metadata"]["resourceVersion"]},
            {"op": "test", "path": "/spec/replicas", "value": 2},
            {"op": "replace", "path": "/spec/replicas", "value": 1},
        ]))
    deadline = time.monotonic() + 180
    while True:
        observed = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
        active_router = [p for p in observed if p["metadata"]["namespace"] == "kagent"
            and p["status"]["phase"] not in {"Succeeded", "Failed"}
            and all(p["metadata"].get("labels", {}).get(k) == v for k, v in router_selector.items())]
        if not any(target_pod(p) for p in observed) and len(active_router) == 1:
            break
        if time.monotonic() > deadline:
            raise ValueError("HOLD graceful adapter/router scale-down; no force delete")
        time.sleep(3)
    kube("kagent", "rollout", "status", "deployment/" + ROUTER, "--timeout=180s")
    router_deployment = json.loads(kube("kagent", "get", "deployment", ROUTER, "-o", "json"))
    if router_deployment["spec"]["replicas"] != 1 or router_deployment["status"].get("readyReplicas") != 1:
        raise ValueError("retained workflow router is not Ready")
    for record in records:
        peer = deployment(record["peer"], record["peer_pool"], record["release"])
        if peer["spec"]["replicas"] != 1 or peer["status"].get("readyReplicas") != 1:
            raise ValueError("retained adapter peer lost readiness")
        slices = json.loads(kube("kagent", "get", "endpointslice", "-l",
            "kubernetes.io/service-name=" + record["service"], "-o", "json"))["items"]
        ready = sum(1 for item in slices for endpoint in item.get("endpoints", []) if endpoint.get("conditions", {}).get("ready") is True)
        if ready < 1:
            raise ValueError("retained release Service has no Ready endpoint")
    current = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    pair = native_reservations(include_backend=False)
    final_reserve = workflow_job_reservations(current, probe=True)
    final_reserve += pair + [pair[0]]
    final = verify_capacity(nodes, current, final_reserve, {}, headroom={"cpu": "200m", "memory": "128Mi"})
    after, after_etag = store.read()
    if after != state or after_etag != etag or not driver.verify_route(after, 0, after["route_revision"]):
        raise ValueError("route/state preservation failure")
    report = {"stage": "serving_releases_single_placement", "snapshot_key": key,
        "targets_scaled_to_zero": [r["target"] for r in records],
        "retained_peers": [r["peer"] for r in records], "sessions_retained": sessions,
        "workflow_gateway_replicas": 2, "workflow_router_replicas": 1,
        "state_unchanged": True, "route_unchanged": True, "candidate_capacity_verified": True,
        "projected_headroom": {n: {k: str(v) for k, v in values.items()} for n, values in final.items()}}
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
