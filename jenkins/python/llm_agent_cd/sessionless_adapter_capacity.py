"""Release one duplicate adapter placement while retaining historical sessions."""

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


REVIEW = "reviewed-history-single-placement-815fe-v3"
RELEASE = "815fe04ba955a000dbe627f2ac0ba1071165840f1b427b21bd0433dc7670bc04"
TARGETS = {
    "rec-ab-815fe04ba955a000dbe6": "ml-system",
}
PEER = "rec-ab-815fe04ba955a000dbe6-cpu"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    if parser.parse_args().image != REVIEW:
        raise ValueError("unreviewed sessionless-history capacity action")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")
    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = json.loads(
        kube("kagent", "get", "deployment", "recsys-workflow-router", "-o", "json")
    )
    os.environ.update(
        AB_SCOPE="workflow",
        AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0][
            "image"
        ],
    )
    check()
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    driver = Driver()
    protected = {
        (state.get(key) or {}).get("release_id")
        for key in ("champion", "previous", "baseline", "pending")
    }
    if state.get("phase") not in {"IDLE", "ROLLED_BACK", "COMPLETED"} or RELEASE in protected:
        raise ValueError("exact terminal state or protected pointer mismatch")
    release_in_state_catalog = RELEASE in state.get("releases", {})
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("rollback route is not verified")
    route = json.loads(
        kube("kagent", "get", "virtualservice", "recsys-workflow-ab", "-o", "json")
    )
    # A pin for an existing historical session is allowed because the Service
    # keeps the independently placed peer endpoint throughout this change.

    def checked_deployment(name: str, pool: str) -> dict:
        obj = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
        env = [
            entry
            for container in obj["spec"]["template"]["spec"]["containers"]
            for entry in container.get("env", [])
            if entry["name"] == "RELEASE_ID"
        ]
        if (
            obj["metadata"].get("labels", {}).get("recsys.ai/owner")
            != "llm-agent-cd"
            or obj["spec"].get("replicas") != 1
            or obj["status"].get("readyReplicas") != 1
            or obj["spec"]["template"]["spec"].get("nodeSelector")
            != {"recsys.ai/pool": pool}
            or env != [{"name": "RELEASE_ID", "value": RELEASE}]
        ):
            raise ValueError("historical adapter identity/readiness drift: " + name)
        return obj

    deployments = [checked_deployment(name, pool) for name, pool in TARGETS.items()]
    peer = checked_deployment(PEER, "cpu-services")
    with driver.db.connect() as connection:
        sessions = connection.execute(
            "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s",
            (RELEASE,),
        ).fetchone()["n"]
        unfinished = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations "
            "WHERE release_id=%s AND finished_at IS NULL",
            (RELEASE,),
        ).fetchone()["n"]
    if unfinished:
        raise ValueError("historical adapter has an unfinished invocation")

    nodes = json.loads(command("kubectl", "get", "nodes", "-o", "json"))["items"]
    pods = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    selectors = [obj["spec"]["selector"]["matchLabels"] for obj in deployments]

    def target(pod: dict) -> bool:
        return (
            pod["metadata"].get("namespace") == "kagent"
            and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            and any(
                all(
                    pod["metadata"].get("labels", {}).get(key) == value
                    for key, value in selector.items()
                )
                for selector in selectors
            )
        )

    planned = [pod for pod in pods if not target(pod)]
    reservations = workflow_job_reservations(planned, probe=True)
    reservations.extend(native_reservations(include_backend=False))
    projected = verify_capacity(
        nodes,
        planned,
        reservations,
        {},
        headroom={"cpu": "200m", "memory": "128Mi"},
    )
    key = (
        "workflow/capacity-windows/sessionless-history-"
        + RELEASE[:20]
        + "-"
        + os.environ["BUILD_NUMBER"]
        + ".json"
    )
    snapshot = {
        "stage": "sessionless_history_adapter_release",
        "build_url": os.environ["BUILD_URL"],
        "state_etag": etag,
        "release_id": RELEASE,
        "release_in_state_catalog": release_in_state_catalog,
        "targets": deployments,
        "retained_peer": peer,
        "sessions": sessions,
        "unfinished": unfinished,
        "projected_headroom": {
            node: {resource: str(value) for resource, value in values.items()}
            for node, values in projected.items()
        },
    }
    store.client.put_object(
        Bucket=store.bucket,
        Key=key,
        Body=json.dumps(snapshot).encode(),
        ContentType="application/json",
        IfNoneMatch="*",
    )
    for obj in deployments:
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
    deadline = time.monotonic() + 180
    while any(
        target(pod)
        for pod in json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))[
            "items"
        ]
    ):
        if time.monotonic() > deadline:
            raise ValueError("HOLD graceful adapter termination; no force delete")
        time.sleep(3)
    peer = checked_deployment(PEER, "cpu-services")
    slices = json.loads(
        kube(
            "kagent",
            "get",
            "endpointslice",
            "-l",
            "kubernetes.io/service-name=rec-ab-815fe04ba955a000dbe6",
            "-o",
            "json",
        )
    )["items"]
    if not any(
        endpoint.get("conditions", {}).get("ready") is True
        for item in slices
        for endpoint in item.get("endpoints", [])
    ):
        raise ValueError("retained historical peer has no Ready Service endpoint")
    current = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))[
        "items"
    ]
    final_reservations = workflow_job_reservations(current, probe=True)
    final_reservations.extend(native_reservations(include_backend=False))
    final = verify_capacity(
        nodes,
        current,
        final_reservations,
        {},
        headroom={"cpu": "200m", "memory": "128Mi"},
    )
    after, after_etag = store.read()
    if (
        after != state
        or after_etag != etag
        or not driver.verify_route(after, 0, after["route_revision"])
    ):
        raise ValueError("state/route preservation failure")
    report = {
        "stage": "sessionless_history_adapter_release",
        "snapshot_key": key,
        "release_id": RELEASE,
        "target_replicas": 0,
        "retained_peer": peer["metadata"]["name"],
        "sessions_retained": sessions,
        "release_backend_and_evidence_retained": True,
        "state_unchanged": True,
        "route_unchanged": True,
        "replacement_adapter_pair_capacity_verified": True,
        "projected_headroom": {
            node: {resource: str(value) for resource, value in values.items()}
            for node, values in final.items()
        },
    }
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
