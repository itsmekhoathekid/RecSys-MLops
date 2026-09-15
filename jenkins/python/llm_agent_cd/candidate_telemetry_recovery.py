"""Recover one candidate after an audited router-evidence infrastructure fault."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import time

from .driver import command, Driver
from .release_guard import check
from .state import StateStore


REVIEW = "reviewed-router-profile-telemetry-recovery-v1"
EXPERIMENT = "wf-11c736e489662a4ab63126a14f839e06"
RELEASE = "b0c946863001e0aa5d5c5b8e087e953863eb22c5a8a84f536f4eb89ca3f5e445"
ROUTER_IMAGE = ("asia-southeast1-docker.pkg.dev/recsys-mlops-506406/recsys/"
                "recsys-llm-ab-router@sha256:"
                "d2d019abf59630939a762ef72d209af38128e7fc8e6204989149fcd39da5ff71")


def validate_recovery(state, evidence):
    if (state.get("phase") != "ROLLED_BACK" or state.get("experiment_id") != EXPERIMENT
            or state.get("pending", {}).get("release_id") != RELEASE
            or state.get("champion", {}).get("release_id") == RELEASE
            or state.get("gate") != {"verdict": "FAIL", "reason": "operator requested rollback"}
            or state.get("cases")):
        raise ValueError("exact telemetry-only rollback state required")
    offline = state.get("offline_evidence", {}).get("cases", [])
    if (len(offline) != 6 or any(not row.get("synced")
            or (row.get("result") or {}).get("verdict") != "PASS" for row in offline)
            or {row.get("variant") for row in offline} != {"control", "candidate"}):
        raise ValueError("six synced offline PASS records required")
    expected = {
        ("live_test", state["baseline"]["release_id"], "HOLD", "child evidence unavailable", False, False): 34,
        ("live_test", RELEASE, "HOLD", "child evidence unavailable", False, False): 3,
    }
    if evidence.get("invocations") != expected or evidence.get("submitted") != 37:
        raise ValueError("only the reviewed 37 telemetry-HOLD invocations may be recovered")
    if evidence.get("compatibility") != {("candidate", "PASS", True): 3,
                                           ("control", "PASS", True): 3}:
        raise ValueError("offline compatibility evidence changed")
    if evidence.get("offline_confirmed") != 6:
        raise ValueError("Langfuse offline score confirmation incomplete")
    if RELEASE in state.get("disabled", []):
        return "pending"
    events = [event for event in state.get("recovery_events", [])
              if event.get("review") == REVIEW and event.get("failed_experiment") == EXPERIMENT
              and event.get("release_id") == RELEASE and event.get("inference_requests") == 0]
    if len(events) != 1:
        raise ValueError("candidate already enabled without exact telemetry recovery evidence")
    return "recovered"


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if args.image != REVIEW:
        raise ValueError("unreviewed telemetry recovery")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")
    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    os.environ.update(AB_SCOPE="workflow", AB_ROUTER_IMAGE=ROUTER_IMAGE)
    check()
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    driver = Driver()
    with driver.db.connect() as connection:
        rows = connection.execute("""SELECT source,release_id,result->>'verdict' verdict,
          result->>'reason' reason,(result->>'error')::boolean error,
          (result->>'contract_failure')::boolean contract_failure,count(*) n
          FROM recsys_ab.invocations WHERE experiment_id=%s
          GROUP BY 1,2,3,4,5,6""", (EXPERIMENT,)).fetchall()
        invocations = {(r["source"], r["release_id"], r["verdict"], r["reason"],
                        r["error"], r["contract_failure"]): r["n"] for r in rows}
        rows = connection.execute("""SELECT variant,result->>'verdict' verdict,
          finished_at IS NOT NULL finished,count(*) n FROM recsys_ab.compatibility_requests
          WHERE experiment_id=%s GROUP BY 1,2,3""", (EXPERIMENT,)).fetchall()
        compatibility = {(r["variant"], r["verdict"], r["finished"]): r["n"] for r in rows}
        submitted = connection.execute("SELECT submitted FROM recsys_ab.live_load_runs WHERE experiment_id=%s",
                                       (EXPERIMENT,)).fetchone()["submitted"]
        offline_confirmed = connection.execute("""SELECT count(*) n FROM recsys_ab.evaluation_outbox
          WHERE experiment_id=%s AND metadata->>'source'='offline' AND confirmed_at IS NOT NULL""",
          (EXPERIMENT,)).fetchone()["n"]
    observed = {"invocations": invocations, "compatibility": compatibility,
                "submitted": submitted, "offline_confirmed": offline_confirmed}
    status = validate_recovery(state, observed)

    deployment = json.loads(driver.kube("get", "deployment", "recsys-workflow-router", "-o", "json"))
    if (deployment["spec"]["template"]["spec"]["containers"][0]["image"] != ROUTER_IMAGE
            or deployment.get("status", {}).get("readyReplicas") != 2):
        raise ValueError("reviewed two-replica router image is not ready")
    pods = json.loads(driver.kube("get", "pods", "-l", "app=recsys-workflow-router", "-o", "json"))["items"]
    names = [pod["metadata"]["name"] for pod in pods if not pod["metadata"].get("deletionTimestamp")]
    with driver.db.connect() as connection:
        beats = connection.execute("""SELECT instance,extract(epoch FROM now()-seen_at) age
          FROM recsys_ab.heartbeat WHERE instance=ANY(%s)""", (names,)).fetchall()
    if len(names) != 2 or len(beats) != 2 or any(float(row["age"]) > 60 for row in beats):
        raise ValueError("fresh heartbeat from both reviewed router pods required")
    from apps.agentic.llm_ab_router.workflow_events import snapshot_events
    if not snapshot_events(state):
        raise ValueError("workflow evidence projection is empty")
    route_state = deepcopy(state)
    if RELEASE not in route_state.setdefault("disabled", []): route_state["disabled"].append(RELEASE)
    if not driver.verify_route(route_state, 0, state["route_revision"]):
        raise ValueError("rollback route not verified")
    driver.verify_release(state["pending"])

    public_counts = {"live_test_total": 37, "control": 34, "candidate": 3,
                     "synthetic": 0, "offline": 6, "offline_confirmed": 6}
    record = {"stage":"candidate_telemetry_recovery", "review":REVIEW,
      "failed_experiment":EXPERIMENT, "release_id":RELEASE, "counts":public_counts,
      "reason":"router catalog projection failed before heartbeat and child evidence inspection",
      "router_image":ROUTER_IMAGE, "build_url":os.environ["BUILD_URL"], "at":time.time(),
      "inference_requests":0}
    key = "workflow/recoveries/" + EXPERIMENT + "/router-profile-telemetry.json"
    if status == "pending":
        store.client.put_object(Bucket=store.bucket, Key=key,
          Body=json.dumps(record,sort_keys=True).encode(),ContentType="application/json",IfNoneMatch="*")
        updated=deepcopy(state)
        updated["disabled"]=[rid for rid in state["disabled"] if rid != RELEASE]
        updated.setdefault("recovery_events",[]).append({**record,"evidence_key":key})
        store.write(updated,etag)
    else:
        persisted=json.loads(store.client.get_object(Bucket=store.bucket,Key=key)["Body"].read())
        for field in ("stage","review","failed_experiment","release_id","counts","reason",
                      "router_image","inference_requests"):
            if persisted.get(field)!=record.get(field): raise ValueError("telemetry recovery evidence mismatch")
        record=persisted
    after,_=store.read()
    if RELEASE in after.get("disabled",[]) or after["champion"]!=state["champion"]:
        raise ValueError("telemetry recovery state verification failed")
    if not driver.verify_route(route_state,0,after["route_revision"]):
        raise ValueError("route changed during telemetry recovery")
    report={**record,"evidence_key":key,"state_recovered":True,
            "champion_unchanged":True,"route_unchanged":True}
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report))


if __name__ == "__main__": main()
