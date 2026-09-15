"""Recover one pre-inference candidate quarantined only by a fixed attestation bug."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import time

from .driver import command, Driver
from .release_guard import check
from .state import StateStore


REVIEW = "reviewed-pre-inference-props-newline-v1"
EXPERIMENT = "wf-4c3a626def29b6646a46b6133dffa100"
RELEASE = "b0c946863001e0aa5d5c5b8e087e953863eb22c5a8a84f536f4eb89ca3f5e445"


def validate_recovery(state, counts):
    if (state.get("phase") != "ROLLED_BACK" or state.get("experiment_id") != EXPERIMENT
            or state.get("pending", {}).get("release_id") != RELEASE
            or state.get("champion", {}).get("release_id") == RELEASE
            or state.get("gate") != {"verdict": "FAIL", "reason": "execution error: ValueError"}):
        raise ValueError("exact pre-inference rollback state required")
    if any(counts.values()) or state.get("cases") or state.get("offline_evidence"):
        raise ValueError("candidate recovery forbidden after inference or evaluation evidence")
    if RELEASE in state.get("disabled", []):
        return "pending"
    events = [event for event in state.get("recovery_events", [])
              if event.get("review") == REVIEW and event.get("failed_experiment") == EXPERIMENT
              and event.get("release_id") == RELEASE and event.get("inference_requests") == 0]
    if len(events) != 1:
        raise ValueError("candidate already enabled without exact recovery evidence")
    return "recovered"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if args.image != REVIEW:
        raise ValueError("unreviewed candidate recovery")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")
    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    os.environ.update(AB_SCOPE="workflow", AB_ROUTER_IMAGE=json.loads(command(
        "kubectl", "-n", "kagent", "get", "deployment", "recsys-workflow-router", "-o", "json"
    ))["spec"]["template"]["spec"]["containers"][0]["image"])
    check()
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    driver = Driver()
    with driver.db.connect() as connection:
        counts = {
            "invocations": connection.execute(
                "SELECT count(*) AS n FROM recsys_ab.invocations WHERE experiment_id=%s OR release_id=%s",
                (EXPERIMENT, RELEASE),
            ).fetchone()["n"],
            "compatibility": connection.execute(
                "SELECT count(*) AS n FROM recsys_ab.compatibility_requests WHERE experiment_id=%s",
                (EXPERIMENT,),
            ).fetchone()["n"],
            "sessions": connection.execute(
                "SELECT count(*) AS n FROM recsys_ab.sessions WHERE release_id=%s", (RELEASE,)
            ).fetchone()["n"],
        }
    status = validate_recovery(state, counts)
    # Removing a release from the quarantine list must not implicitly mutate
    # the live VirtualService. Verify against the exact pre-recovery route;
    # the next experiment owns adding the zero-weight pinned release route.
    route_state = deepcopy(state)
    if RELEASE not in route_state.setdefault("disabled", []):
        route_state["disabled"].append(RELEASE)
    if not driver.verify_route(route_state, 0, state["route_revision"]):
        raise ValueError("rollback route not verified")
    # This includes the fixed one-newline /props normalization, agent cards,
    # backend health and exact immutable object checks; it performs no inference.
    driver.verify_release(state["pending"])
    evidence = {"stage": "candidate_release_recovery", "review": REVIEW,
        "failed_experiment": EXPERIMENT, "release_id": RELEASE, "counts": counts,
        "reason": "llama.cpp /props strips one POSIX trailing newline from --chat-template-file",
        "build_url": os.environ["BUILD_URL"], "at": time.time(), "inference_requests": 0}
    key = "workflow/recoveries/" + EXPERIMENT + "/props-newline.json"
    if status == "pending":
        store.client.put_object(Bucket=store.bucket, Key=key,
            Body=json.dumps(evidence, sort_keys=True).encode(), ContentType="application/json",
            IfNoneMatch="*")
        updated = deepcopy(state)
        updated["disabled"] = [release_id for release_id in state["disabled"] if release_id != RELEASE]
        updated.setdefault("recovery_events", []).append({**evidence, "evidence_key": key})
        store.write(updated, etag)
    else:
        persisted = json.loads(store.client.get_object(Bucket=store.bucket, Key=key)["Body"].read())
        for field in ("stage", "review", "failed_experiment", "release_id", "counts", "reason",
                      "inference_requests"):
            if persisted.get(field) != evidence.get(field):
                raise ValueError("recovery evidence does not match resumed state")
        evidence = persisted
    after, _ = store.read()
    if RELEASE in after.get("disabled", []) or after["champion"] != state["champion"]:
        raise ValueError("candidate recovery state verification failed")
    if not driver.verify_route(route_state, 0, after["route_revision"]):
        raise ValueError("route changed during candidate recovery")
    report = {**evidence, "evidence_key": key, "state_recovered": True,
        "champion_unchanged": True, "route_unchanged": True}
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
