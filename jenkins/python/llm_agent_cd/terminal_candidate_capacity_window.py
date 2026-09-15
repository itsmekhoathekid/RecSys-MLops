"""Reversible capacity window for the stock-ADK Qwen2.5 terminal candidate.

This action is intentionally narrower than a general rightsizer.  It accepts
only the reviewed production baseline, workload identities and CPU request
transitions below.  Every mutation is journaled before it is applied.  A
readiness or exact-candidate preflight failure restores the values observed at
entry before the Jenkins build is allowed to fail.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import time

from apps.agentic.llm_ab_router.trigger import candidate_from_config
from .driver import Driver, command
from .provision import kube
from .release import digest
from .release_guard import check
from .state import StateStore


WINDOW = "reviewed-qwen25-terminal-capacity-v1"
BASELINE = "2458bfb95a8e3310adeea29ab39fe2f5ebf963d4f48060356fdc8c8fb84385c6"
CATALOG = "configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m-tools-v3.json"
FIXTURES = "configs/llm-ab/workflow-cases-v11.json"

# These releases are historical, are not a serving pointer and had zero
# sessions at review time.  The job re-proves all three facts from state and
# PostgreSQL before it changes a replica field.
SESSIONLESS_ADAPTERS = (
    "0617db4c88e87d9a3ce906137cf64c2402b1b835b92a2f6170dae3065299496d",
    "25a9165703c266d5c363d5d1a8f4d41c057eea16b93f2e8c10cdcfbb085ec211",
    "55cab0570ae3b57e443daaa52c81339a9dd0d6657af47398b9b0b16d11664267",
)

WORKER_POOLS = (
    "recsys-coordinator-sandbox-pool",
    "recsys-context-sandbox-pool",
    "recsys-recommendation-sandbox-pool",
)

UNUSED_HTTP_ADDONS = (
    ("keda-add-ons-http-controller-manager", "keda-add-ons-http-operator"),
    ("keda-add-ons-http-external-scaler", "keda-add-ons-http-external-scaler"),
    ("keda-add-ons-http-interceptor", "keda-add-ons-http-interceptor"),
)

# Namespace, kind, workload, container, reviewed old CPU, reviewed new CPU.
# Memory, limits, replicas, command and every other field are preserved.
CPU_TARGETS = (
    ("langfuse", "deployment", "langfuse-web", "langfuse-web", "200m", "100m"),
    ("langfuse", "deployment", "langfuse-worker", "langfuse-worker", "200m", "100m"),
    ("langfuse", "statefulset", "langfuse-postgresql", "postgresql", "200m", "100m"),
    ("langfuse", "deployment", "langfuse-redis", "langfuse-redis", "100m", "50m"),
    ("langfuse", "statefulset", "langfuse-keeper-0", "clickhouse-keeper", "100m", "50m"),
    ("langfuse", "deployment", "clickhouse-operator-controller-manager", "manager", "100m", "50m"),
    ("keda", "deployment", "keda-admission-webhooks", "keda-admission-webhooks", "100m", "50m"),
    ("keda", "deployment", "keda-operator", "keda-operator", "100m", "50m"),
    ("keda", "deployment", "keda-operator-metrics-apiserver", "keda-operator-metrics-apiserver", "100m", "50m"),
)


def _get(namespace: str, kind: str, name: str) -> dict:
    return json.loads(kube(namespace, "get", kind, name, "-o", "json"))


def _ready_workload(value: dict) -> None:
    replicas = value["spec"].get("replicas")
    status = value.get("status", {})
    if replicas != 1 or status.get("readyReplicas") != 1:
        raise ValueError(
            "capacity target is not single-replica Ready: "
            + value["metadata"]["namespace"]
            + "/"
            + value["metadata"]["name"]
        )
    if status.get("observedGeneration", 0) < value["metadata"].get("generation", 0):
        raise ValueError("capacity target generation is not observed")


def _container_index(value: dict, container: str) -> int:
    matches = [
        index
        for index, item in enumerate(value["spec"]["template"]["spec"]["containers"])
        if item.get("name") == container
    ]
    if len(matches) != 1:
        raise ValueError("reviewed container identity drift: " + container)
    return matches[0]


def _cpu(value: dict, container: str) -> str:
    index = _container_index(value, container)
    return value["spec"]["template"]["spec"]["containers"][index].get(
        "resources", {}
    ).get("requests", {}).get("cpu", "")


def _wait_workload(namespace: str, kind: str, name: str, uid: str) -> dict:
    command(
        "kubectl",
        "-n",
        namespace,
        "rollout",
        "status",
        kind + "/" + name,
        "--timeout=300s",
    )
    value = _get(namespace, kind, name)
    if value["metadata"].get("uid") != uid:
        raise ValueError("capacity target was recreated: " + namespace + "/" + name)
    _ready_workload(value)
    return value


def _patch_cpu(
    namespace: str,
    kind: str,
    name: str,
    container: str,
    expected: str,
    desired: str,
    uid: str,
) -> dict:
    value = _get(namespace, kind, name)
    if value["metadata"].get("uid") != uid:
        raise ValueError("capacity target UID drift: " + namespace + "/" + name)
    _ready_workload(value)
    index = _container_index(value, container)
    if _cpu(value, container) != expected:
        raise ValueError("CPU request drift: " + namespace + "/" + name)
    path = f"/spec/template/spec/containers/{index}/resources/requests/cpu"
    kube(
        namespace,
        "patch",
        kind,
        name,
        "--type=json",
        "-p",
        json.dumps(
            [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": value["metadata"]["resourceVersion"],
                },
                {
                    "op": "test",
                    "path": f"/spec/template/spec/containers/{index}/name",
                    "value": container,
                },
                {"op": "test", "path": path, "value": expected},
                {"op": "replace", "path": path, "value": desired},
            ]
        ),
    )
    result = _wait_workload(namespace, kind, name, uid)
    if _cpu(result, container) != desired:
        raise ValueError("CPU request did not converge: " + namespace + "/" + name)
    return result


def _scaled_pool(name: str, bounds: set[tuple[int, int]]) -> dict:
    scaled = _get("kagent", "scaledobject", name)
    worker = _get("kagent", "workerpool", name)
    minimum = scaled["spec"].get("minReplicaCount")
    maximum = scaled["spec"].get("maxReplicaCount")
    ready = next(
        (
            condition.get("status")
            for condition in scaled.get("status", {}).get("conditions", [])
            if condition.get("type") == "Ready"
        ),
        None,
    )
    if (
        (minimum, maximum) not in bounds
        or ready != "True"
        or scaled["spec"].get("scaleTargetRef")
        != {
            "apiVersion": "ate.dev/v1alpha1",
            "kind": "WorkerPool",
            "name": name,
        }
        or worker["spec"].get("replicas") != minimum
        or worker.get("status", {}).get("replicas") != minimum
        or worker["spec"]["template"].get("nodeSelector")
        != {"cloud.google.com/gke-nodepool": "recsys-mlops-cpu"}
        or worker["spec"]["template"]["resources"]["requests"].get("cpu") != "100m"
    ):
        raise ValueError("reviewed ScaledObject/WorkerPool identity drift: " + name)
    return {"scaled_object": scaled, "worker_pool": worker}


def _patch_scaled_object(
    name: str,
    expected: tuple[int, int],
    desired: tuple[int, int],
    uid: str,
) -> dict:
    value = _get("kagent", "scaledobject", name)
    if value["metadata"].get("uid") != uid:
        raise ValueError("ScaledObject UID drift: " + name)
    actual = (
        value["spec"].get("minReplicaCount"),
        value["spec"].get("maxReplicaCount"),
    )
    if actual == desired:
        return value
    if actual != expected:
        raise ValueError("ScaledObject replica bounds drift: " + name)
    kube(
        "kagent",
        "patch",
        "scaledobject",
        name,
        "--type=json",
        "-p",
        json.dumps(
            [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": value["metadata"]["resourceVersion"],
                },
                {
                    "op": "test",
                    "path": "/spec/minReplicaCount",
                    "value": expected[0],
                },
                {
                    "op": "test",
                    "path": "/spec/maxReplicaCount",
                    "value": expected[1],
                },
                {
                    "op": "replace",
                    "path": "/spec/minReplicaCount",
                    "value": desired[0],
                },
                {
                    "op": "replace",
                    "path": "/spec/maxReplicaCount",
                    "value": desired[1],
                },
            ]
        ),
    )
    return _get("kagent", "scaledobject", name)


def _wait_worker_pool(name: str, desired: int, uid: str) -> dict:
    # All three ScaledObjects are patched before this wait begins.  Their
    # reviewed HPA stabilization window is 300 seconds, so use one bounded
    # convergence window rather than serially spending it three times.
    deadline = time.monotonic() + 420
    while True:
        current = _get("kagent", "workerpool", name)
        if (
            current["metadata"].get("uid") == uid
            and current["spec"].get("replicas") == desired
            and current.get("status", {}).get("replicas") == desired
        ):
            command(
                "kubectl",
                "-n",
                "kagent",
                "rollout",
                "status",
                "deployment/" + name,
                "--timeout=120s",
            )
            return current
        if time.monotonic() > deadline:
            raise ValueError("WorkerPool replica transition did not converge: " + name)
        time.sleep(3)


def _adapter(release_id: str, replicas: set[int]) -> dict:
    name = "rec-ab-" + release_id[:20] + "-cpu"
    value = _get("kagent", "deployment", name)
    env = [
        entry
        for container in value["spec"]["template"]["spec"]["containers"]
        for entry in container.get("env", [])
        if entry.get("name") == "RELEASE_ID"
    ]
    if (
        value["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
        # Kubernetes label values are limited to 63 characters; the full
        # release identity is independently checked in the immutable env.
        or value["metadata"].get("labels", {}).get("recsys.ai/release")
        != release_id[:63]
        or value["spec"].get("replicas") not in replicas
        or (
            value["spec"].get("replicas") == 1
            and value.get("status", {}).get("readyReplicas") != 1
        )
        or value["spec"]["template"]["spec"].get("nodeSelector")
        != {"recsys.ai/pool": "cpu-services"}
        or env != [{"name": "RELEASE_ID", "value": release_id}]
    ):
        raise ValueError("sessionless adapter identity/readiness drift: " + name)
    return value


def _wait_adapter_absent(value: dict) -> None:
    selector = value["spec"]["selector"]["matchLabels"]
    deadline = time.monotonic() + 300
    while True:
        pods = json.loads(
            command(
                "kubectl",
                "-n",
                value["metadata"]["namespace"],
                "get",
                "pods",
                "-o",
                "json",
            )
        )["items"]
        admitted = [
            pod
            for pod in pods
            if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            and all(
                pod["metadata"].get("labels", {}).get(key) == wanted
                for key, wanted in selector.items()
            )
        ]
        if not admitted:
            return
        if time.monotonic() > deadline:
            raise ValueError("HOLD adapter termination; no force delete")
        time.sleep(3)


def _http_addon(name: str, container: str, replicas: set[int]) -> dict:
    value = _get("keda", "deployment", name)
    actual = value["spec"].get("replicas")
    index = _container_index(value, container)
    if (
        value["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != "Helm"
        or actual not in replicas
        or (actual == 1 and value.get("status", {}).get("readyReplicas") != 1)
        or value["spec"]["template"]["spec"]["containers"][index]
        .get("resources", {})
        .get("requests", {})
        .get("cpu")
        != "25m"
    ):
        raise ValueError("reviewed unused KEDA HTTP add-on drift: " + name)
    return value


def _patch_http_addon(
    name: str, container: str, expected: int, desired: int, uid: str
) -> dict:
    value = _http_addon(name, container, {expected})
    if value["metadata"].get("uid") != uid:
        raise ValueError("KEDA HTTP add-on UID drift: " + name)
    kube(
        "keda",
        "patch",
        "deployment",
        name,
        "--type=json",
        "-p",
        json.dumps(
            [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": value["metadata"]["resourceVersion"],
                },
                {"op": "test", "path": "/spec/replicas", "value": expected},
                {"op": "replace", "path": "/spec/replicas", "value": desired},
            ]
        ),
    )
    if desired == 0:
        _wait_adapter_absent(value)
    else:
        command(
            "kubectl",
            "-n",
            "keda",
            "rollout",
            "status",
            "deployment/" + name,
            "--timeout=300s",
        )
    result = _http_addon(name, container, {desired})
    if result["metadata"].get("uid") != uid:
        raise ValueError("KEDA HTTP add-on was recreated: " + name)
    return result


def _patch_adapter(release_id: str, expected: int, desired: int, uid: str) -> dict:
    value = _adapter(release_id, {expected})
    if value["metadata"].get("uid") != uid:
        raise ValueError("sessionless adapter UID drift")
    name = value["metadata"]["name"]
    kube(
        "kagent",
        "patch",
        "deployment",
        name,
        "--type=json",
        "-p",
        json.dumps(
            [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": value["metadata"]["resourceVersion"],
                },
                {"op": "test", "path": "/spec/replicas", "value": expected},
                {"op": "replace", "path": "/spec/replicas", "value": desired},
            ]
        ),
    )
    if desired == 0:
        _wait_adapter_absent(value)
    else:
        command(
            "kubectl",
            "-n",
            "kagent",
            "rollout",
            "status",
            "deployment/" + name,
            "--timeout=300s",
        )
    result = _adapter(release_id, {desired})
    if result["metadata"].get("uid") != uid:
        raise ValueError("sessionless adapter was recreated")
    return result


def _candidate(state: dict) -> dict:
    llm = json.loads(Path(CATALOG).read_text())
    config = {
        "schema_version": 1,
        "scope": "workflow",
        "baseline_workflow_release_id": state["champion"]["release_id"],
        "global_generation": deepcopy(state["champion"]["global_generation"]),
        "llm_release_ref": digest(llm),
        "experiment_type": "llm_only",
        "policy_ref": "workflow-live-test",
    }
    return candidate_from_config(state["champion"], config, llm)


def _restore(actions: list[dict]) -> None:
    failures = []
    for action in reversed(actions):
        try:
            if action["type"] == "cpu":
                current = _get(action["namespace"], action["kind"], action["name"])
                if current["metadata"].get("uid") != action["uid"]:
                    raise ValueError("capacity target UID drift during restore")
                if _cpu(current, action["container"]) != action["original"]:
                    _patch_cpu(
                        action["namespace"],
                        action["kind"],
                        action["name"],
                        action["container"],
                        action["desired"],
                        action["original"],
                        action["uid"],
                    )
                else:
                    _wait_workload(
                        action["namespace"], action["kind"], action["name"], action["uid"]
                    )
            elif action["type"] == "scaled_pool":
                scaled = _get("kagent", "scaledobject", action["name"])
                actual = (
                    scaled["spec"].get("minReplicaCount"),
                    scaled["spec"].get("maxReplicaCount"),
                )
                if actual != tuple(action["original"]):
                    _patch_scaled_object(
                        action["name"],
                        tuple(action["desired"]),
                        tuple(action["original"]),
                        action["uid"],
                    )
                _wait_worker_pool(
                    action["name"], action["original"][0], action["worker_uid"]
                )
            elif action["type"] == "http_addon":
                current = _http_addon(
                    action["name"], action["container"], {0, 1}
                )
                if current["spec"].get("replicas") != action["original"]:
                    _patch_http_addon(
                        action["name"],
                        action["container"],
                        action["desired"],
                        action["original"],
                        action["uid"],
                    )
            else:
                current = _adapter(action["release_id"], {0, 1})
                if current["spec"].get("replicas") != action["original"]:
                    _patch_adapter(
                        action["release_id"],
                        action["desired"],
                        action["original"],
                        action["uid"],
                    )
        except Exception as error:  # continue restoring independent targets
            failures.append(action.get("name", action.get("release_id", "unknown")) + ": " + str(error))
    if failures:
        raise RuntimeError("ROLLBACK_FAILED: " + "; ".join(failures))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    if parser.parse_args().image != WINDOW:
        raise ValueError("unreviewed terminal-candidate capacity action")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")

    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = _get("kagent", "deployment", "recsys-workflow-router")
    os.environ.update(
        AB_SCOPE="workflow",
        AB_SECRET_NAME="recsys-workflow-runtime",
        AB_ALLOW_LIVE_TEST="true",
        AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0]["image"],
    )
    check("workflow")
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    if (
        state.get("phase") != "IDLE"
        or not state.get("activated")
        or state.get("experiment_id")
        or state.get("champion", {}).get("release_id") != BASELINE
        or state.get("baseline", {}).get("release_id") != BASELINE
        or state.get("pending", {}).get("release_id") != BASELINE
    ):
        raise ValueError("exact activated IDLE v28 baseline required")

    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("workflow route is not the verified baseline route")
    with driver.db.connect() as connection:
        unfinished = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations WHERE finished_at IS NULL"
        ).fetchone()["n"]
        dispatching = connection.execute(
            "SELECT count(*) n FROM recsys_ab.trigger_requests "
            "WHERE status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED','RUNNING')"
        ).fetchone()["n"]
        release_counts = {
            release_id: {
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
            for release_id in SESSIONLESS_ADAPTERS
        }
    if unfinished or dispatching:
        raise ValueError("active workflow or dispatch blocks capacity window")
    if any(counts["sessions"] or counts["unfinished"] for counts in release_counts.values()):
        raise ValueError("reviewed historical adapter gained a session or unfinished invocation")

    pointers = {
        (state.get(key) or {}).get("release_id")
        for key in ("champion", "previous", "baseline", "pending")
    }
    releases = state.get("releases", {})
    for release_id in SESSIONLESS_ADAPTERS:
        if release_id in pointers:
            raise ValueError("sessionless adapter became a serving pointer")
        recorded = releases.get(release_id)
        # Releases pruned from the active route map are valid historical
        # orphans.  Their immutable live label plus PostgreSQL session proof is
        # the authority for scaling the adapter only.  If the state still has
        # an entry, however, it must map to this exact release identity.
        if recorded is not None and recorded.get("release_id") != release_id:
            raise ValueError("historical state release mapping mismatch")

    http_scaled = json.loads(
        command(
            "kubectl",
            "get",
            "httpscaledobjects.http.keda.sh",
            "-A",
            "-o",
            "json",
        )
    )["items"]
    if http_scaled:
        raise ValueError("KEDA HTTP add-on has a live HTTPScaledObject consumer")

    adapters = [_adapter(release_id, {0, 1}) for release_id in SESSIONLESS_ADAPTERS]
    pools = [_scaled_pool(name, {(1, 1), (2, 3)}) for name in WORKER_POOLS]
    http_addons = [
        _http_addon(name, container, {0, 1})
        for name, container in UNUSED_HTTP_ADDONS
    ]
    workloads = []
    for namespace, kind, name, container, old_cpu, new_cpu in CPU_TARGETS:
        value = _get(namespace, kind, name)
        _ready_workload(value)
        if _cpu(value, container) not in {old_cpu, new_cpu}:
            raise ValueError("reviewed CPU transition drift: " + namespace + "/" + name)
        workloads.append(value)

    candidate = _candidate(state)
    if not re.fullmatch(r"[0-9a-f]{64}", candidate["release_id"]):
        raise ValueError("candidate identity construction failed")
    journal = (
        "workflow/capacity-windows/terminal-candidate-"
        + BASELINE
        + "-"
        + os.environ["BUILD_NUMBER"]
        + ".json"
    )
    snapshot = {
        "stage": "terminal_candidate_capacity_intent",
        "window": WINDOW,
        "build_url": os.environ["BUILD_URL"],
        "state_etag": etag,
        "route_revision": state["route_revision"],
        "baseline_release_id": BASELINE,
        "candidate_release_id": candidate["release_id"],
        "candidate_llm_version_id": candidate["llm_version_id"],
        "release_counts": release_counts,
        "adapters": adapters,
        "worker_pools": pools,
        "http_scaled_object_count": len(http_scaled),
        "unused_http_addons": http_addons,
        "cpu_targets": workloads,
    }
    store.client.put_object(
        Bucket=store.bucket,
        Key=journal,
        Body=json.dumps(snapshot, sort_keys=True).encode(),
        ContentType="application/json",
        IfNoneMatch="*",
    )

    actions: list[dict] = []
    try:
        for (name, container), value in zip(UNUSED_HTTP_ADDONS, http_addons):
            if value["spec"].get("replicas") == 1:
                action = {
                    "type": "http_addon",
                    "name": name,
                    "container": container,
                    "original": 1,
                    "desired": 0,
                    "uid": value["metadata"]["uid"],
                }
                _patch_http_addon(name, container, 1, 0, action["uid"])
                actions.append(action)

        for release_id, value in zip(SESSIONLESS_ADAPTERS, adapters):
            if value["spec"].get("replicas") == 1:
                action = {
                    "type": "adapter",
                    "release_id": release_id,
                    "name": value["metadata"]["name"],
                    "original": 1,
                    "desired": 0,
                    "uid": value["metadata"]["uid"],
                }
                _patch_adapter(release_id, 1, 0, action["uid"])
                actions.append(action)

        pool_actions = []
        for name, value in zip(WORKER_POOLS, pools):
            scaled = value["scaled_object"]
            worker = value["worker_pool"]
            if scaled["spec"].get("minReplicaCount") == 2:
                action = {
                    "type": "scaled_pool",
                    "name": name,
                    "original": [2, 3],
                    "desired": [1, 1],
                    "uid": scaled["metadata"]["uid"],
                    "worker_uid": worker["metadata"]["uid"],
                }
                _patch_scaled_object(name, (2, 3), (1, 1), action["uid"])
                actions.append(action)
                pool_actions.append(action)
        for action in pool_actions:
            _wait_worker_pool(action["name"], 1, action["worker_uid"])

        for target, value in zip(CPU_TARGETS, workloads):
            namespace, kind, name, container, old_cpu, new_cpu = target
            if _cpu(value, container) == old_cpu:
                action = {
                    "type": "cpu",
                    "namespace": namespace,
                    "kind": kind,
                    "name": name,
                    "container": container,
                    "original": old_cpu,
                    "desired": new_cpu,
                    "uid": value["metadata"]["uid"],
                }
                _patch_cpu(namespace, kind, name, container, old_cpu, new_cpu, action["uid"])
                actions.append(action)

        driver.preflight(
            state["champion"],
            candidate,
            json.loads(Path(FIXTURES).read_text()),
        )
        after, after_etag = store.read()
        if (
            after != state
            or after_etag != etag
            or not driver.verify_route(after, 0, after["route_revision"])
        ):
            raise ValueError("state/route preservation failure")
    except Exception as original:
        try:
            _restore(actions)
        except Exception as rollback:
            raise RuntimeError(str(rollback)) from original
        raise

    report = {
        "stage": "terminal_candidate_capacity",
        "window": WINDOW,
        "journal_key": journal,
        "baseline_release_id": BASELINE,
        "candidate_release_id": candidate["release_id"],
        "candidate_llm_version_id": candidate["llm_version_id"],
        "sessionless_adapters_scaled_to_zero": [
            action["name"] for action in actions if action["type"] == "adapter"
        ],
        "worker_pools_scaled_to_one": [
            action["name"] for action in actions if action["type"] == "scaled_pool"
        ],
        "unused_keda_http_addons_paused": [
            action["name"] for action in actions if action["type"] == "http_addon"
        ],
        "http_scaled_object_consumers": 0,
        "cpu_requests_rightsized": [
            action["namespace"] + "/" + action["name"]
            for action in actions
            if action["type"] == "cpu"
        ],
        "exact_candidate_capacity_preflight": "PASS",
        "state_unchanged": True,
        "route_unchanged": True,
        "replicas_memory_limits_preserved": True,
        "inference_requests": 0,
    }
    target = Path(".llm-agent-cd/evaluation-preparation.json")
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
