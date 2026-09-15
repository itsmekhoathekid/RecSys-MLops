"""Recover the exact candidate quarantined by an offline wiring mismatch.

The failed experiment completed six direct compatibility calls and zero
workflow requests. Recovery only journals verified evidence and removes that
candidate from the quarantine set; it never invokes a model, agent, or tool.
"""
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import time

from botocore.exceptions import ClientError

from .driver import Driver
from .provision import kube
from .release import digest
from .release_guard import check
from .state import StateStore


REVIEW = "reviewed-frozen-offline-suite-recovery-v1"
EXPERIMENT = "wf-ea32d7f844ca548526cb6b0f11415530"
CONTROL = "ea6a8e9318cb41073745254893fb64d4c5cdec26850959a7b8498ba7ce7ab2f2"
CANDIDATE = "68ceae428cd646327bb9557e682952da9711557e54572aad7ddde41a3167457c"
OLD_FIXTURE_CHECKSUM = "523da483f4c6008540543a9c17c629364821cc4fa81ecd24d0cca408fcb86b0e"


def validate(state, evidence):
    if (state.get("phase") != "ROLLED_BACK"
            or state.get("experiment_id") != EXPERIMENT
            or state.get("champion", {}).get("release_id") != CONTROL
            or state.get("pending", {}).get("release_id") != CANDIDATE
            or state.get("gate") != {
                "verdict": "FAIL", "reason": "execution error: ValueError"}
            or state.get("cases")
            or state.get("offline_evidence", {}).get("cases") != []):
        raise ValueError("exact offline-wiring rollback state required")
    expected = {
        "compatibility": {
            ("candidate", "PASS", True): 3,
            ("control", "PASS", True): 3,
        },
        "fixture_checksums": {OLD_FIXTURE_CHECKSUM},
        "offline_confirmed": 6,
        "invocations": 0,
        "live_load_rows": 0,
        "candidate_sessions": 0,
    }
    if evidence != expected:
        raise ValueError("offline-only recovery evidence mismatch")
    if CANDIDATE in state.get("disabled", []):
        return "pending"
    events = [event for event in state.get("recovery_events", [])
              if event.get("review") == REVIEW
              and event.get("failed_experiment") == EXPERIMENT
              and event.get("release_id") == CANDIDATE
              and event.get("inference_requests") == 0]
    if len(events) != 1:
        raise ValueError("candidate enabled without exact offline-wiring recovery")
    return "recovered"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if args.image != REVIEW:
        raise ValueError("unreviewed offline-wiring recovery")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")
    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = json.loads(kube(
        "kagent", "get", "deployment", "recsys-workflow-router", "-o", "json"))
    os.environ.update(
        AB_SCOPE="workflow",
        AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0]["image"],
    )
    check("workflow")
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    driver = Driver()
    with driver.db.connect() as connection:
        rows = connection.execute("""SELECT variant,result->>'verdict' verdict,
            finished_at IS NOT NULL finished,count(*) n
            FROM recsys_ab.compatibility_requests WHERE experiment_id=%s
            GROUP BY 1,2,3""", (EXPERIMENT,)).fetchall()
        compatibility = {
            (row["variant"], row["verdict"], row["finished"]): row["n"]
            for row in rows
        }
        fixture_checksums = {row["fixture_checksum"] for row in
            connection.execute("""SELECT DISTINCT fixture_checksum
                FROM recsys_ab.compatibility_requests WHERE experiment_id=%s""",
                (EXPERIMENT,)).fetchall()}
        confirmed = connection.execute("""SELECT count(*) n
            FROM recsys_ab.evaluation_outbox WHERE experiment_id=%s
              AND metadata->>'source'='offline' AND confirmed_at IS NOT NULL""",
            (EXPERIMENT,)).fetchone()["n"]
        invocations = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations WHERE experiment_id=%s",
            (EXPERIMENT,)).fetchone()["n"]
        live_load_rows = connection.execute(
            "SELECT count(*) n FROM recsys_ab.live_load_runs WHERE experiment_id=%s",
            (EXPERIMENT,)).fetchone()["n"]
        candidate_sessions = connection.execute(
            "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s",
            (CANDIDATE,)).fetchone()["n"]
    evidence = {
        "compatibility": compatibility,
        "fixture_checksums": fixture_checksums,
        "offline_confirmed": confirmed,
        "invocations": invocations,
        "live_load_rows": live_load_rows,
        "candidate_sessions": candidate_sessions,
    }
    status = validate(state, evidence)
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("verified rollback route required")
    driver.verify_release(state["champion"])
    driver.verify_release(state["pending"])
    record = {
        "stage": "candidate_offline_wiring_recovery",
        "review": REVIEW,
        "failed_experiment": EXPERIMENT,
        "release_id": CANDIDATE,
        "reason": "offline Job image and Jenkins source used different suite revisions",
        "old_fixture_checksum": OLD_FIXTURE_CHECKSUM,
        "evidence_checksum": digest(sorted(compatibility.items())),
        "counts": {
            "offline": 6,
            "offline_confirmed": 6,
            "workflow": 0,
            "live_test": 0,
            "synthetic": 0,
            "candidate_sessions": 0,
        },
        "frozen_suite_required_for_next_experiment": True,
        "build_url": os.environ["BUILD_URL"],
        "at": time.time(),
        "inference_requests": 0,
    }
    key = ("workflow/recoveries/" + EXPERIMENT
           + "/frozen-offline-suite-v1.json")
    if status == "pending":
        body = json.dumps(record, sort_keys=True).encode()
        try:
            store.client.put_object(
                Bucket=store.bucket, Key=key, Body=body,
                ContentType="application/json", IfNoneMatch="*")
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                raise
            persisted = json.loads(store.client.get_object(
                Bucket=store.bucket, Key=key)["Body"].read())
            for field in (
                    "stage", "review", "failed_experiment", "release_id", "reason",
                    "old_fixture_checksum", "counts",
                    "frozen_suite_required_for_next_experiment", "inference_requests"):
                if persisted.get(field) != record.get(field):
                    raise ValueError("offline recovery journal conflict")
            record = persisted
        updated = deepcopy(state)
        updated["disabled"] = [release_id for release_id in state.get("disabled", [])
                               if release_id != CANDIDATE]
        updated.setdefault("recovery_events", []).append(
            {**record, "evidence_key": key})
        store.write(updated, etag)
    else:
        record = json.loads(store.client.get_object(
            Bucket=store.bucket, Key=key)["Body"].read())
    after, _ = store.read()
    if (CANDIDATE in after.get("disabled", [])
            or after["champion"] != state["champion"]):
        raise ValueError("offline recovery state verification failed")
    if not driver.verify_route(state, 0, after["route_revision"]):
        raise ValueError("route changed during offline recovery")
    report = {
        **record,
        "evidence_key": key,
        "state_recovered": True,
        "champion_unchanged": True,
        "route_unchanged": True,
    }
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(
        json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
