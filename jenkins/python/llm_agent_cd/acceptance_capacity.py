"""Journal and release exact adapter capacity for the stock A/B acceptance run."""

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


REVIEW = "reviewed-stock-acceptance-adapters-v1"
POINTERS = {
    "champion": "83ddb3a25137dc96632d1ee0b16cd1a35da2a9d764e0e993a8fc1222b086ac0e",
    "previous": "04bb90820951c5af16c898c18a3cb61b514b1470255dd9972791c4c284b6c10d",
    "pending": "55cab0570ae3b57e443daaa52c81339a9dd0d6657af47398b9b0b16d11664267",
}
SESSIONLESS = (
    "2eb78c4f4450ab0d026e4f7bcf0802a0b4e4bcd025a7c080abdedc5c6c5207d2",
    "b98b14c7fda65c18724c753bf44697c1a919c90b31926b4e5b2a2bede92b1dda",
    "f10eb3828016e227d48e3cddc7a96bccc4a8c9bd0b655014c197efee49bdb35f",
)


def _deployment(name: str, release_id: str, pool: str) -> dict:
    obj = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
    env = [
        entry
        for container in obj["spec"]["template"]["spec"]["containers"]
        for entry in container.get("env", [])
        if entry["name"] == "RELEASE_ID"
    ]
    if (
        obj["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
        or obj["spec"].get("replicas") != 1
        or obj["status"].get("readyReplicas") != 1
        or obj["spec"]["template"]["spec"].get("nodeSelector")
        != {"recsys.ai/pool": pool}
        or env != [{"name": "RELEASE_ID", "value": release_id}]
    ):
        raise ValueError("adapter identity/readiness drift: " + name)
    return obj


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    if parser.parse_args().image != REVIEW:
        raise ValueError("unreviewed stock acceptance capacity action")
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
    if state.get("phase") != "ROLLED_BACK":
        raise ValueError("exact rolled-back terminal state required")
    for key, release_id in POINTERS.items():
        if (state.get(key) or {}).get("release_id") != release_id:
            raise ValueError("workflow pointer drift: " + key)
    if POINTERS["pending"] not in state.get("disabled", []):
        raise ValueError("pending candidate is not quarantined")
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("rollback route is not verified")
    route = json.loads(
        kube("kagent", "get", "virtualservice", "recsys-workflow-ab", "-o", "json")
    )
    route_spec = json.dumps(route["spec"])
    if any(release_id in route_spec for release_id in SESSIONLESS):
        raise ValueError("sessionless history unexpectedly remains routed")

    targets = []
    retained_peers = []
    for release_id in POINTERS.values():
        base = "rec-ab-" + release_id[:20]
        targets.append(_deployment(base, release_id, "ml-system"))
        retained_peers.append(_deployment(base + "-cpu", release_id, "cpu-services"))
    for release_id in SESSIONLESS:
        base = "rec-ab-" + release_id[:20]
        targets.extend(
            (
                _deployment(base, release_id, "ml-system"),
                _deployment(base + "-cpu", release_id, "cpu-services"),
            )
        )

    counts = {}
    with driver.db.connect() as connection:
        for release_id in (*POINTERS.values(), *SESSIONLESS):
            counts[release_id] = {
                "sessions": connection.execute(
                    "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s",
                    (release_id,),
                ).fetchone()["n"],
                "unfinished": connection.execute(
                    "SELECT count(*) n FROM recsys_ab.invocations "
                    "WHERE release_id=%s AND finished_at IS NULL",
                    (release_id,),
                ).fetchone()["n"],
            }
    if any(value["unfinished"] for value in counts.values()):
        raise ValueError("target release has an unfinished invocation")
    if any(counts[release_id]["sessions"] for release_id in SESSIONLESS):
        raise ValueError("sessionless history gained a session")

    nodes = json.loads(command("kubectl", "get", "nodes", "-o", "json"))["items"]
    pods = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    selectors = [obj["spec"]["selector"]["matchLabels"] for obj in targets]

    def target_pod(pod: dict) -> bool:
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

    planned = [pod for pod in pods if not target_pod(pod)]
    reservations = workflow_job_reservations(planned, probe=True)
    reservations.extend(native_reservations(include_backend=False))
    projected = verify_capacity(
        nodes,
        planned,
        reservations,
        {},
        headroom={"cpu": "200m", "memory": "128Mi"},
    )
    key = "workflow/capacity-windows/stock-acceptance-" + os.environ["BUILD_NUMBER"] + ".json"
    snapshot = {
        "stage": "stock_acceptance_adapter_capacity",
        "build_url": os.environ["BUILD_URL"],
        "state_etag": etag,
        "targets": targets,
        "retained_peers": retained_peers,
        "counts": counts,
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
    for obj in targets:
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
        target_pod(pod)
        for pod in json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))[
            "items"
        ]
    ):
        if time.monotonic() > deadline:
            raise ValueError("HOLD graceful adapter termination; no force delete")
        time.sleep(3)

    for peer in retained_peers:
        live = _deployment(
            peer["metadata"]["name"],
            next(
                entry["value"]
                for container in peer["spec"]["template"]["spec"]["containers"]
                for entry in container.get("env", [])
                if entry["name"] == "RELEASE_ID"
            ),
            "cpu-services",
        )
        service = live["metadata"]["name"].removesuffix("-cpu")
        slices = json.loads(
            kube(
                "kagent",
                "get",
                "endpointslice",
                "-l",
                "kubernetes.io/service-name=" + service,
                "-o",
                "json",
            )
        )["items"]
        if not any(
            endpoint.get("conditions", {}).get("ready") is True
            for item in slices
            for endpoint in item.get("endpoints", [])
        ):
            raise ValueError("retained pointer service has no Ready endpoint")

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
        "stage": "stock_acceptance_adapter_capacity",
        "snapshot_key": key,
        "targets_scaled_to_zero": [obj["metadata"]["name"] for obj in targets],
        "retained_pointer_peers": [
            obj["metadata"]["name"] for obj in retained_peers
        ],
        "release_backends_sessions_and_evidence_retained": True,
        "state_unchanged": True,
        "route_unchanged": True,
        "full_stock_baseline_capacity_verified": True,
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
