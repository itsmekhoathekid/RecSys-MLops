"""Gracefully release adapters of one exact failed, never-activated baseline."""
import argparse
import json
import os
from pathlib import Path
import re
import time

from .driver import Driver
from .provision import kube
from .release import digest
from .release_guard import check
from .state import StateStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True,
                        help="Exact immutable baseline preparation object key")
    args = parser.parse_args()
    if not re.fullmatch(r"workflow/baseline-preparations/[0-9a-f]{64}/[0-9]+\.json", args.image):
        raise ValueError("exact baseline preparation key required")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")
    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = json.loads(kube("kagent", "get", "deployment", "recsys-workflow-router", "-o", "json"))
    os.environ.update(AB_SCOPE="workflow",
        AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0]["image"])
    check()
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    driver = Driver()
    prepared = json.loads(store.client.get_object(Bucket=store.bucket, Key=args.image)["Body"].read())
    baseline = prepared["baseline"]
    release_id = baseline["release_id"]
    if args.image.split("/")[-2] != release_id or prepared.get("activated") is not False:
        raise ValueError("preparation identity mismatch")
    referenced = {state.get(key, {}).get("release_id") for key in
                  ("champion", "previous", "baseline", "pending")}
    referenced.update(state.get("releases", {}).keys())
    if state.get("phase") not in {"ROLLED_BACK", "IDLE", "COMPLETED"} or release_id in referenced:
        raise ValueError("prepared baseline is active or retained in state")
    if not driver.verify_route(state, state["verified_weight"], state["route_revision"]):
        raise ValueError("current serving route is not verified")
    route = json.loads(kube("kagent", "get", "virtualservice", "recsys-workflow-ab", "-o", "json"))
    if release_id in json.dumps(route["spec"]):
        raise ValueError("failed baseline is still routable")
    contract = prepared.get("audit", {}).get("prompt_contract")
    probe_versions = {
        "workflow-stock-a2a-v3": "v5",
        "workflow-stock-a2a-v4": "v6",
        "workflow-stock-a2a-v5": "v7",
        "workflow-stock-a2a-v6": "v8",
        "workflow-stock-a2a-v7": "v9",
        "workflow-stock-a2a-v8": "v10",
        "workflow-stock-a2a-v9": "v11",
        "workflow-stock-a2a-v10": "v12",
        "workflow-stock-a2a-v11": "v13",
        "workflow-stock-a2a-v12": "v14",
        "workflow-stock-a2a-v13": "v15",
        "workflow-stock-a2a-v14": "v16",
        "workflow-stock-a2a-v15": "v17",
        "workflow-stock-a2a-v16": "v18",
        "workflow-stock-a2a-v17": "v19",
        "workflow-stock-a2a-v18": "v20",
        "workflow-stock-a2a-v19": "v21",
        "workflow-stock-a2a-v20": "v22",
        "workflow-stock-a2a-v21": "v23",
        "workflow-stock-a2a-v22": "v24",
        "workflow-stock-a2a-v23": "v25",
        "workflow-stock-a2a-v24": "v26",
        "workflow-stock-a2a-v25": "v27",
        "workflow-stock-a2a-v26": "v28",
        "workflow-stock-a2a-v27": "v28",
        "workflow-stock-a2a-v28": "v29",
    }
    prompt_probe_versions = {
        "workflow-coordinator-native-sequential-v31": "v32",
        "workflow-coordinator-native-isolated-sequential-v32": "v33",
    }
    if contract in prompt_probe_versions:
        probe_version = prompt_probe_versions[contract]
        probe_ids = [
            "prompt-baseline-coordinator-recommendation-a2a-" + probe_version,
            "prompt-baseline-coordinator-context-a2a-" + probe_version,
            "prompt-baseline-coordinator-composite-a2a-" + probe_version,
        ]
    else:
        if contract not in probe_versions:
            raise ValueError("unreviewed prompt contract for failed preparation")
        probe_version = probe_versions[contract]
        probe_ids = [
            "stock-recommendation-" + probe_version,
            "stock-context-exact-null-" + probe_version,
        ]
        if contract in {"workflow-stock-a2a-v6", "workflow-stock-a2a-v7", "workflow-stock-a2a-v8", "workflow-stock-a2a-v9", "workflow-stock-a2a-v10", "workflow-stock-a2a-v11", "workflow-stock-a2a-v12", "workflow-stock-a2a-v13", "workflow-stock-a2a-v14", "workflow-stock-a2a-v15", "workflow-stock-a2a-v16", "workflow-stock-a2a-v17", "workflow-stock-a2a-v18", "workflow-stock-a2a-v19", "workflow-stock-a2a-v20", "workflow-stock-a2a-v21", "workflow-stock-a2a-v22", "workflow-stock-a2a-v23", "workflow-stock-a2a-v24", "workflow-stock-a2a-v25", "workflow-stock-a2a-v26", "workflow-stock-a2a-v27", "workflow-stock-a2a-v28"}:
            if contract in {"workflow-stock-a2a-v7", "workflow-stock-a2a-v8", "workflow-stock-a2a-v9", "workflow-stock-a2a-v10", "workflow-stock-a2a-v11", "workflow-stock-a2a-v12", "workflow-stock-a2a-v13", "workflow-stock-a2a-v14", "workflow-stock-a2a-v15", "workflow-stock-a2a-v16", "workflow-stock-a2a-v17", "workflow-stock-a2a-v18", "workflow-stock-a2a-v19", "workflow-stock-a2a-v20", "workflow-stock-a2a-v21", "workflow-stock-a2a-v22", "workflow-stock-a2a-v23", "workflow-stock-a2a-v24", "workflow-stock-a2a-v25", "workflow-stock-a2a-v26", "workflow-stock-a2a-v27", "workflow-stock-a2a-v28"}:
                probe_ids.append("stock-context-exact-chunk-" + probe_version)
            probe_ids += [
                "stock-coordinator-recommendation-a2a-owner-" + probe_version,
                "stock-coordinator-context-a2a-owner-" + probe_version,
                "stock-coordinator-composite-a2a-owner-" + probe_version,
            ]
        else:
            probe_ids += ["stock-coordinator-a2a-owner-" + probe_version]
    evidence = []
    verdicts = []
    for probe_id in probe_ids:
        key = "workflow/runtime-preflights/" + release_id + "/" + probe_id + "/result.json"
        result = json.loads(store.client.get_object(Bucket=store.bucket, Key=key)["Body"].read())
        if (result.get("verdict") not in {"PASS", "FAIL"} or result.get("release_id") != release_id
                or result.get("source") != "infrastructure_test"
                or result.get("offline_requests") != 0 or result.get("synthetic_requests") != 0):
            raise ValueError("exact terminal probe evidence required")
        verdicts.append(result["verdict"])
        evidence.append({"key": key, "checksum": digest(result)})
    if "FAIL" not in verdicts:
        raise ValueError("cannot release capacity for a fully passing preparation")
    targets = []
    for name in ("rec-ab-" + release_id[:20], "rec-ab-" + release_id[:20] + "-cpu"):
        obj = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
        env = [entry for c in obj["spec"]["template"]["spec"]["containers"]
               for entry in c.get("env", []) if entry["name"] == "RELEASE_ID"]
        if (obj["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
                or obj["spec"].get("replicas") != 1
                or obj["status"].get("readyReplicas") != 1
                or env != [{"name": "RELEASE_ID", "value": release_id}]):
            raise ValueError("failed adapter identity/readiness drift: " + name)
        targets.append(obj)
    with driver.db.connect() as connection:
        sessions = connection.execute(
            "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s", (release_id,)).fetchone()["n"]
        unfinished = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations WHERE release_id=%s AND finished_at IS NULL",
            (release_id,)).fetchone()["n"]
    if sessions or unfinished:
        raise ValueError("failed baseline has a session or unfinished invocation")
    journal_key = "workflow/capacity-windows/failed-prepared-" + release_id + "-" + os.environ["BUILD_NUMBER"] + ".json"
    store.client.put_object(Bucket=store.bucket, Key=journal_key,
        Body=json.dumps({"stage":"failed_prepared_baseline_adapter_release",
            "build_url":os.environ["BUILD_URL"],"state_etag":etag,
            "preparation_key":args.image,"release_id":release_id,
            "prompt_contract":contract,"probe_evidence":evidence,
            "probe_verdicts":verdicts,"sessions":sessions,"unfinished":unfinished,
            "targets":[{"name":x["metadata"]["name"],"resourceVersion":x["metadata"]["resourceVersion"]} for x in targets]}).encode(),
        ContentType="application/json", IfNoneMatch="*")
    for obj in targets:
        kube("kagent", "patch", "deployment", obj["metadata"]["name"], "--type=json", "-p",
             json.dumps([{"op":"test","path":"/metadata/resourceVersion","value":obj["metadata"]["resourceVersion"]},
                         {"op":"test","path":"/spec/replicas","value":1},
                         {"op":"replace","path":"/spec/replicas","value":0}]))
    deadline = time.monotonic() + 180
    while any(json.loads(kube("kagent", "get", "deployment", x["metadata"]["name"], "-o", "json"))
              ["status"].get("replicas", 0) for x in targets):
        if time.monotonic() >= deadline:
            raise ValueError("HOLD graceful adapter scale-down; no force delete")
        time.sleep(3)
    after, after_etag = store.read()
    if after != state or after_etag != etag or not driver.verify_route(
            after, after["verified_weight"], after["route_revision"]):
        raise ValueError("state/route changed during capacity release")
    report={"stage":"failed_prepared_baseline_adapter_release","release_id":release_id,
            "journal_key":journal_key,"target_replicas":0,"state_unchanged":True,
            "route_unchanged":True,"release_agents_backend_and_evidence_retained":True}
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
