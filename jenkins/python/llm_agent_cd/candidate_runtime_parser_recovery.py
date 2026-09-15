"""Recover a candidate quarantined only by the pre-v4 workflow parser.

The failed run never assigned a live-test or synthetic conversation to the
candidate.  This job is deliberately tied to the exact immutable evidence and
router digest below; it cannot become a generic quarantine bypass.
"""
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import time

from .driver import Driver
from .release_guard import check
from .state import StateStore


REVIEW = "reviewed-runtime-aware-router-parser-v4-recovery-v1"
EXPERIMENT = "wf-33bf2ca18b5ff6564d9ed2a16d5dc759"
CONTROL = "83ddb3a25137dc96632d1ee0b16cd1a35da2a9d764e0e993a8fc1222b086ac0e"
RELEASE = "55cab0570ae3b57e443daaa52c81339a9dd0d6657af47398b9b0b16d11664267"
ROUTER_IMAGE = ("asia-southeast1-docker.pkg.dev/recsys-mlops-506406/recsys/"
                "recsys-llm-ab-router@sha256:"
                "bbeb108f9fe941bc5b08de67f14b00c405c51a61a1cb7b698724d33eba4d15c6")


def validate(state, evidence):
    if (state.get("phase") != "ROLLED_BACK" or state.get("experiment_id") != EXPERIMENT
            or state.get("champion", {}).get("release_id") != CONTROL
            or state.get("pending", {}).get("release_id") != RELEASE
            or state.get("gate") != {"verdict": "FAIL", "reason": "operator requested rollback"}
            or state.get("cases")):
        raise ValueError("exact runtime-parser rollback state required")
    offline = state.get("offline_evidence", {}).get("cases", [])
    if (len(offline) != 6 or any(not row.get("synced")
            or (row.get("result") or {}).get("verdict") != "PASS" for row in offline)
            or {row.get("variant") for row in offline} != {"control", "candidate"}):
        raise ValueError("six synced offline PASS records required")
    expected_invocations = {
        ("live_test", CONTROL, "HOLD", "child evidence unavailable", False, False): 4,
    }
    if (evidence["invocations"] != expected_invocations or evidence["submitted"] != 4
            or evidence["candidate_sessions"] != 0 or evidence["synthetic"] != 0):
        raise ValueError("only the reviewed four control telemetry-HOLD calls may be recovered")
    if evidence["compatibility"] != {("candidate", "PASS", True): 3,
                                      ("control", "PASS", True): 3}:
        raise ValueError("offline compatibility evidence changed")
    if evidence["offline_confirmed"] != 6:
        raise ValueError("Langfuse offline score confirmation incomplete")
    if RELEASE in state.get("disabled", []):
        return "pending"
    recovered = [event for event in state.get("recovery_events", [])
                 if event.get("review") == REVIEW and event.get("failed_experiment") == EXPERIMENT
                 and event.get("release_id") == RELEASE and event.get("inference_requests") == 0]
    if len(recovered) != 1:
        raise ValueError("candidate already enabled without exact parser recovery evidence")
    return "recovered"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if args.image != REVIEW:
        raise ValueError("unreviewed runtime parser recovery")
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
          FROM recsys_ab.invocations WHERE experiment_id=%s GROUP BY 1,2,3,4,5,6""",
                                  (EXPERIMENT,)).fetchall()
        invocations = {(row["source"], row["release_id"], row["verdict"], row["reason"],
                        row["error"], row["contract_failure"]): row["n"] for row in rows}
        rows = connection.execute("""SELECT variant,result->>'verdict' verdict,
          finished_at IS NOT NULL finished,count(*) n FROM recsys_ab.compatibility_requests
          WHERE experiment_id=%s GROUP BY 1,2,3""", (EXPERIMENT,)).fetchall()
        compatibility = {(row["variant"], row["verdict"], row["finished"]): row["n"] for row in rows}
        submitted = connection.execute("SELECT submitted FROM recsys_ab.live_load_runs WHERE experiment_id=%s",
                                       (EXPERIMENT,)).fetchone()["submitted"]
        candidate_sessions = connection.execute("SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s",
                                                (RELEASE,)).fetchone()["n"]
        synthetic = connection.execute("SELECT count(*) n FROM recsys_ab.invocations WHERE experiment_id=%s AND source='synthetic'",
                                       (EXPERIMENT,)).fetchone()["n"]
        offline_confirmed = connection.execute("""SELECT count(*) n FROM recsys_ab.evaluation_outbox
          WHERE experiment_id=%s AND metadata->>'source'='offline' AND confirmed_at IS NOT NULL""",
                                               (EXPERIMENT,)).fetchone()["n"]
    evidence = {"invocations": invocations, "compatibility": compatibility,
                "submitted": submitted, "candidate_sessions": candidate_sessions,
                "synthetic": synthetic, "offline_confirmed": offline_confirmed}
    status = validate(state, evidence)

    deployment = json.loads(driver.kube("get", "deployment", "recsys-workflow-router", "-o", "json"))
    if (deployment["spec"]["template"]["spec"]["containers"][0]["image"] != ROUTER_IMAGE
            or deployment.get("status", {}).get("readyReplicas") != deployment["spec"].get("replicas")):
        raise ValueError("runtime-aware router image is not fully Ready")
    # The live route must still be the verified rollback route. Removing the
    # quarantine does not mutate it; the next experiment owns adding its pin.
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("rollback route not verified")
    driver.verify_release(state["pending"])

    record = {"stage": "candidate_runtime_parser_recovery", "review": REVIEW,
              "failed_experiment": EXPERIMENT, "release_id": RELEASE,
              "counts": {"live_test_control_hold": 4, "candidate_online": 0,
                         "synthetic": 0, "offline": 6, "offline_confirmed": 6},
              "reason": "pre-v4 router rejected the immutable runtime field before child evidence inspection",
              "router_image": ROUTER_IMAGE, "build_url": os.environ["BUILD_URL"],
              "at": time.time(), "inference_requests": 0}
    key = "workflow/recoveries/" + EXPERIMENT + "/runtime-aware-parser-v4.json"
    if status == "pending":
        store.client.put_object(Bucket=store.bucket, Key=key,
            Body=json.dumps(record, sort_keys=True).encode(), ContentType="application/json", IfNoneMatch="*")
        updated = deepcopy(state)
        updated["disabled"] = [release_id for release_id in state["disabled"] if release_id != RELEASE]
        updated.setdefault("recovery_events", []).append({**record, "evidence_key": key})
        store.write(updated, etag)
    else:
        persisted = json.loads(store.client.get_object(Bucket=store.bucket, Key=key)["Body"].read())
        for field in ("stage", "review", "failed_experiment", "release_id", "counts", "reason",
                      "router_image", "inference_requests"):
            if persisted.get(field) != record.get(field):
                raise ValueError("runtime parser recovery evidence mismatch")
        record = persisted
    after, _ = store.read()
    route_state = deepcopy(after)
    route_state.setdefault("disabled", []).append(RELEASE)
    route_state["disabled"] = sorted(set(route_state["disabled"]))
    if (RELEASE in after.get("disabled", []) or after["champion"] != state["champion"]
            or not driver.verify_route(route_state, 0, after["route_revision"])):
        raise ValueError("runtime parser recovery state/route verification failed")
    report = {**record, "evidence_key": key, "state_recovered": True,
              "champion_unchanged": True, "route_unchanged": True}
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
