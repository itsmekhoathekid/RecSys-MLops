"""Journaled capacity window for the stock-ADK Qwen2.5 production A/B run.

Only idle Kubeflow Pipelines control-plane deployments are paused.  Kubeflow
itself remains installed and its storage is retained.  The independent legacy
Recommendation A/B router keeps one Ready worker.  A failed exact-candidate
preflight restores every changed replica field before returning failure.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import time

from botocore.exceptions import ClientError

from apps.agentic.llm_ab_router.trigger import candidate_from_config
from .driver import Driver, command
from .manifests import backend_resources, resources
from .provision import kube, secret
from .release import digest
from .release_guard import check
from .state import StateStore


WINDOW = "stock-adk-qwen25-terminal-llm-only-v2"
TARGETS = (
    ("kubeflow", "ml-pipeline"),
    ("kubeflow", "ml-pipeline-persistenceagent"),
    ("kubeflow", "ml-pipeline-visualizationserver"),
    ("kubeflow", "workflow-controller"),
    ("kubeflow", "kuberay-operator"),
    ("kubeflow", "controller-manager"),
    ("kubeflow", "mysql"),
)
LEGACY_POOL = "recsys-ab-router-pool"
CATALOG = "configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m-tools-v3.json"
FIXTURES = "configs/llm-ab/workflow-cases-v11.json"


def _deployment(namespace, name):
    value = json.loads(kube(namespace, "get", "deployment", name, "-o", "json"))
    if value["spec"].get("replicas") not in {0, 1}:
        raise ValueError("capacity target replica drift: " + namespace + "/" + name)
    if value["spec"].get("replicas") == 1 and value.get("status", {}).get("readyReplicas") != 1:
        raise ValueError("capacity target is not Ready: " + namespace + "/" + name)
    return value


def _patch_replicas(namespace, kind, name, expected, desired):
    value = json.loads(kube(namespace, "get", kind, name, "-o", "json"))
    if value["spec"].get("replicas") == desired:
        return
    if value["spec"].get("replicas") != expected:
        raise ValueError("replica drift before capacity mutation: " + namespace + "/" + name)
    kube(namespace, "patch", kind, name, "--type=json", "-p", json.dumps([
        {"op": "test", "path": "/metadata/resourceVersion", "value": value["metadata"]["resourceVersion"]},
        {"op": "test", "path": "/spec/replicas", "value": expected},
        {"op": "replace", "path": "/spec/replicas", "value": desired},
    ]))


def _wait_paused(selectors):
    deadline = time.monotonic() + 300
    while True:
        pods = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
        remaining = [p for p in pods if p.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            and any(p["metadata"].get("namespace") == namespace
                and all(p["metadata"].get("labels", {}).get(k) == v for k, v in selector.items())
                for namespace, selector in selectors)]
        if not remaining:
            return
        if time.monotonic() > deadline:
            raise ValueError("HOLD graceful Kubeflow control-plane pause; no force delete")
        time.sleep(3)


def _restore(targets, pool_replicas):
    for value in targets:
        _patch_replicas(value["metadata"]["namespace"], "deployment",
                        value["metadata"]["name"], 0, 1)
    _patch_replicas("kagent", "workerpool", LEGACY_POOL, 1, pool_replicas)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if args.image != WINDOW:
        raise ValueError("unreviewed capacity window")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")

    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    os.environ.update(
        AB_SCOPE="workflow",
        AB_SECRET_NAME="recsys-workflow-runtime",
        AB_ALLOW_LIVE_TEST="true",
        AB_ROUTER_IMAGE=json.loads(kube("kagent", "get", "deployment",
            "recsys-workflow-router", "-o", "json"))["spec"]["template"]["spec"]["containers"][0]["image"],
    )
    check("workflow")
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    if (state.get("phase") != "IDLE" or not state.get("activated")
            or state.get("experiment_id")):
        raise ValueError("verified activated IDLE workflow required")
    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("workflow route is not the verified baseline route")
    with driver.db.connect() as connection:
        unfinished = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations WHERE finished_at IS NULL"
        ).fetchone()["n"]
        dispatching = connection.execute("""SELECT count(*) n FROM recsys_ab.trigger_requests
            WHERE status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED','RUNNING')""").fetchone()["n"]
    if unfinished or dispatching:
        raise ValueError("active workflow/dispatch exists before capacity window")
    workflows = json.loads(command("kubectl", "get", "workflows.argoproj.io", "-A", "-o", "json"))["items"]
    if any(w.get("status", {}).get("phase") in {"Pending", "Running"} for w in workflows):
        raise ValueError("active Argo workflow blocks Kubeflow Pipelines pause")

    targets = [_deployment(namespace, name) for namespace, name in TARGETS]
    pool = json.loads(kube("kagent", "get", "workerpool", LEGACY_POOL, "-o", "json"))
    if pool["spec"].get("replicas") not in {1, 2}:
        raise ValueError("legacy Recommendation router pool replica drift")
    if pool.get("status", {}).get("replicas") != pool["spec"]["replicas"]:
        raise ValueError("legacy Recommendation router pool is not Ready")
    original_pool_replicas = 2
    key = "workflow/capacity-windows/" + WINDOW + "-" + state["champion"]["release_id"] + ".json"
    snapshot = {"stage": "capacity_intent", "window": WINDOW, "build_url": os.environ["BUILD_URL"],
        "state_etag": etag, "champion_release_id": state["champion"]["release_id"],
        "targets": targets, "legacy_pool": pool, "kubeflow_storage_retained": True,
        "active_argo_workflows": 0, "unfinished_invocations": unfinished,
        "active_dispatches": dispatching}
    body = json.dumps(snapshot, sort_keys=True).encode()
    try:
        store.client.put_object(Bucket=store.bucket, Key=key, Body=body,
            ContentType="application/json", IfNoneMatch="*")
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
            raise
        old = json.loads(store.client.get_object(Bucket=store.bucket, Key=key)["Body"].read())
        if (old.get("window") != WINDOW
                or old.get("champion_release_id") != state["champion"]["release_id"]):
            raise ValueError("capacity journal conflict")

    changed = False
    try:
        for value in targets:
            if value["spec"].get("replicas") == 1:
                _patch_replicas(value["metadata"]["namespace"], "deployment",
                                value["metadata"]["name"], 1, 0)
                changed = True
        if pool["spec"].get("replicas") == 2:
            _patch_replicas("kagent", "workerpool", LEGACY_POOL, 2, 1)
            changed = True
        selectors = [(value["metadata"]["namespace"], value["spec"]["selector"]["matchLabels"])
                     for value in targets]
        _wait_paused(selectors)
        deadline = time.monotonic() + 300
        while True:
            live_pool = json.loads(kube("kagent", "get", "workerpool", LEGACY_POOL, "-o", "json"))
            if live_pool["spec"].get("replicas") == 1 and live_pool.get("status", {}).get("replicas") == 1:
                break
            if time.monotonic() > deadline:
                raise ValueError("legacy Recommendation router pool did not converge to one Ready worker")
            time.sleep(3)

        llm = json.loads(Path(CATALOG).read_text())
        config = {"schema_version": 1, "scope": "workflow",
            "baseline_workflow_release_id": state["champion"]["release_id"],
            "global_generation": deepcopy(state["champion"]["global_generation"]),
            "llm_release_ref": digest(llm), "experiment_type": "llm_only",
            "policy_ref": "workflow-live-test"}
        candidate = candidate_from_config(state["champion"], config, llm)
        driver.preflight(state["champion"], candidate,
                         json.loads(Path(FIXTURES).read_text()))
        after, after_etag = store.read()
        if after != state or after_etag != etag or not driver.verify_route(after, 0, after["route_revision"]):
            raise ValueError("route/state preservation failure")
    except Exception:
        if changed:
            _restore(targets, original_pool_replicas)
        raise

    report = {"stage": "stock_ab_capacity_window", "window": WINDOW,
        "snapshot_key": key, "full_exact_candidate_preflight": "PASS",
        "candidate_release_id": candidate["release_id"],
        "candidate_llm_version_id": candidate["llm_version_id"],
        "kubeflow_pipeline_control_plane_paused": [name for _, name in TARGETS],
        "kubeflow_storage_retained": True, "kubeflow_not_fully_disabled": True,
        "legacy_recommendation_router_workers": 1,
        "state_unchanged": True, "route_unchanged": True,
        "inference_requests": 0}
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
