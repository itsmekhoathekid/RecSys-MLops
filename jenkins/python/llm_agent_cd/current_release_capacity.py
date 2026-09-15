"""Release duplicate E2 adapter reservations without retiring a release.

The active champion and current rolled-back candidate each retain their N2
adapter. This action is state-derived and preserves sessions and backends.
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


REVIEW = "reviewed-current-release-single-placement-v1"


def release_ids(state):
    champion = (state.get("champion") or {}).get("release_id")
    pending = (state.get("pending") or {}).get("release_id")
    if (state.get("phase") != "ROLLED_BACK" or not champion or not pending
            or champion == pending or pending not in state.get("disabled", [])):
        raise ValueError("exact champion/rolled-back-candidate state required")
    return champion, pending


def deployment(name, pool, release_id):
    obj = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
    env = [entry for container in obj["spec"]["template"]["spec"]["containers"]
           for entry in container.get("env", []) if entry["name"] == "RELEASE_ID"]
    if (obj["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
            or obj["spec"].get("replicas") not in {0, 1}
            or obj["spec"]["template"]["spec"].get("nodeSelector") != {"recsys.ai/pool": pool}
            or env != [{"name": "RELEASE_ID", "value": release_id}]):
        raise ValueError("adapter identity drift: " + name)
    return obj


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    if parser.parse_args().image != REVIEW:
        raise ValueError("unreviewed current-release capacity action")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")
    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = json.loads(kube("kagent", "get", "deployment", "recsys-workflow-router", "-o", "json"))
    os.environ.update(AB_SCOPE="workflow",
                      AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0]["image"])
    check()
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    champion, pending = release_ids(state)
    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("rollback route is not verified")

    records = []
    for release_id in (champion, pending):
        base = "rec-ab-" + release_id[:20]
        target = deployment(base, "ml-system", release_id)
        peer = deployment(base + "-cpu", "cpu-services", release_id)
        if peer["spec"]["replicas"] != 1 or peer["status"].get("readyReplicas") != 1:
            raise ValueError("retained N2 adapter is not Ready: " + base)
        if target["spec"]["replicas"] == 1 and target["status"].get("readyReplicas") != 1:
            raise ValueError("E2 adapter is not Ready before scale-down: " + base)
        records.append({"release_id": release_id, "service": base,
                        "target": target, "peer": peer})

    # Both LLMs used by the next baseline/A-B sequence are already admitted:
    # the baseline revision keeps the champion LLM identity, and the next
    # candidate reuses the rolled-back candidate's immutable LLM identity.
    # Attest those live backends before excluding a fictitious third backend
    # from the capacity projection.
    backends = []
    for release_id in (champion, pending):
        value = (state.get("releases", {}).get(release_id)
                 or state.get("champion") if release_id == champion
                 else state.get("pending"))
        if not value or not value.get("bindings", {}).get(
                "coordinator", {}).get("managed_backend"):
            raise ValueError("managed workflow backend identity required")
        backend_name = "rec-llm-" + value["llm_version_id"][:20]
        backend = json.loads(kube("kagent", "get", "deployment",
                                  backend_name, "-o", "json"))
        if (backend["metadata"].get("labels", {}).get("recsys.ai/owner")
                != "llm-agent-cd"
                or backend["spec"].get("replicas") != 1
                or backend.get("status", {}).get("readyReplicas") != 1):
            raise ValueError("admitted workflow backend is not Ready: "
                             + backend_name)
        backends.append(backend)

    with driver.db.connect() as connection:
        unfinished = {release_id: connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations "
            "WHERE release_id=%s AND finished_at IS NULL", (release_id,)
        ).fetchone()["n"] for release_id in (champion, pending)}
    if any(unfinished.values()):
        raise ValueError("serving release has an unfinished invocation")

    nodes = json.loads(command("kubectl", "get", "nodes", "-o", "json"))["items"]
    pods = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    selectors = [record["target"]["spec"]["selector"]["matchLabels"] for record in records]

    def target_pod(pod):
        return (pod["metadata"]["namespace"] == "kagent"
                and pod["status"]["phase"] not in {"Succeeded", "Failed"}
                and any(all(pod["metadata"].get("labels", {}).get(k) == v
                            for k, v in selector.items()) for selector in selectors))

    planned = [pod for pod in pods if not target_pod(pod)]
    reservations = workflow_job_reservations(planned, probe=True)
    reservations.extend(native_reservations(include_backend=False))
    projected = verify_capacity(nodes, planned, reservations, {},
                                headroom={"cpu": "200m", "memory": "128Mi"})
    object_key = ("workflow/capacity-windows/current-release-single-placement-"
                  + os.environ["BUILD_NUMBER"] + ".json")
    snapshot = {
        "stage": "current_release_single_placement",
        "build_url": os.environ["BUILD_URL"],
        "state_etag": etag,
        "champion_release_id": champion,
        "pending_release_id": pending,
        "targets": [record["target"] for record in records],
        "retained_peers": [record["peer"] for record in records],
        "retained_ready_backends": backends,
        "unfinished": unfinished,
        "projected_headroom": {
            node: {resource: str(value) for resource, value in values.items()}
            for node, values in projected.items()
        },
    }
    store.client.put_object(Bucket=store.bucket, Key=object_key,
                            Body=json.dumps(snapshot).encode(),
                            ContentType="application/json", IfNoneMatch="*")

    for record in records:
        target = record["target"]
        if target["spec"]["replicas"] == 1:
            kube("kagent", "patch", "deployment", target["metadata"]["name"],
                 "--type=json", "-p", json.dumps([
                     {"op": "test", "path": "/metadata/resourceVersion",
                      "value": target["metadata"]["resourceVersion"]},
                     {"op": "test", "path": "/spec/replicas", "value": 1},
                     {"op": "replace", "path": "/spec/replicas", "value": 0},
                 ]))

    deadline = time.monotonic() + 180
    while True:
        observed = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
        if not any(target_pod(pod) for pod in observed):
            break
        if time.monotonic() > deadline:
            raise ValueError("HOLD graceful E2 adapter scale-down; no force delete")
        time.sleep(3)

    for record in records:
        peer = deployment(record["peer"]["metadata"]["name"], "cpu-services",
                          record["release_id"])
        if peer["spec"]["replicas"] != 1 or peer["status"].get("readyReplicas") != 1:
            raise ValueError("retained N2 adapter lost readiness")
        slices = json.loads(kube("kagent", "get", "endpointslice", "-l",
            "kubernetes.io/service-name=" + record["service"], "-o", "json"))["items"]
        ready = sum(1 for item in slices for endpoint in item.get("endpoints", [])
                    if endpoint.get("conditions", {}).get("ready") is True)
        if ready < 1:
            raise ValueError("retained release Service has no Ready endpoint")

    current = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    final_reservations = workflow_job_reservations(current, probe=True)
    final_reservations.extend(native_reservations(include_backend=False))
    final = verify_capacity(nodes, current, final_reservations, {},
                            headroom={"cpu": "200m", "memory": "128Mi"})
    after, after_etag = store.read()
    if after != state or after_etag != etag or not driver.verify_route(
            after, 0, after["route_revision"]):
        raise ValueError("route/state preservation failure")
    report = {
        "stage": "current_release_single_placement",
        "snapshot_key": object_key,
        "targets_scaled_to_zero": [record["target"]["metadata"]["name"] for record in records],
        "retained_peers": [record["peer"]["metadata"]["name"] for record in records],
        "state_unchanged": True,
        "route_unchanged": True,
        "release_backend_and_sessions_retained": True,
        "retained_ready_backends": [backend["metadata"]["name"]
                                    for backend in backends],
        "full_stock_baseline_capacity_verified": True,
        "projected_headroom": {
            node: {resource: str(value) for resource, value in values.items()}
            for node, values in final.items()
        },
    }
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(
        json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
