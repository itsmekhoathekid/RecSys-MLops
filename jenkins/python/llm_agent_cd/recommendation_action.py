"""One-build, one-action Jenkins executor for Recommendation LLM A/B.

This module deliberately contains no rollout decision loop.  The controller
registers an immutable action in PostgreSQL; Jenkins verifies it, performs the
single mutation under the production lock, records evidence, then exits.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

from apps.agentic.llm_ab_router.database import Database
from jenkins.python.llm_agent_cd.cleanup import TerminalCleanup
from jenkins.python.llm_agent_cd.driver import Driver
from jenkins.python.llm_agent_cd.engine import Engine
from jenkins.python.llm_agent_cd.release import digest
from jenkins.python.llm_agent_cd.state import StateStore
from jenkins.python.model_cd.storage import parse_s3_uri, s3_client

ACTION_PHASES = {
    "prepare": {"IDLE", "COMPLETED", "ROLLED_BACK"},
    "route": {"OFFLINE_PASS", "CANARY", "AB"},
    "promote": {"VERIFY"},
    "rollback": {"DEPLOY", "OFFLINE", "OFFLINE_PASS", "PREPARE_FAILED",
                 "CANARY", "AB", "VERIFY", "ROUTING", "ROLLING_BACK"},
    "cleanup": {"COMPLETED", "ROLLED_BACK"},
}
ROUTE_PHASE = {10: "CANARY", 50: "AB", 100: "VERIFY"}


def load(path: str) -> dict | list:
    if path.startswith("s3://"):
        bucket, key = parse_s3_uri(path)
        return json.loads(s3_client().get_object(Bucket=bucket, Key=key)["Body"].read())
    return json.loads(Path(path).read_text())


def load_environment() -> None:
    path = os.environ.get("AB_ENV_FILE")
    if not path:
        return
    values = load(path)
    allowed = {
        "AB_DATABASE_URL", "AB_INTERNAL_TOKEN", "AB_ROUTER_URL", "AB_NAMESPACE",
        "AB_SECRET_NAME", "AB_CASE_TICKET_KEY", "AB_EXTERNAL_A2A_URL",
        "AB_LIVE_TEST_TOKEN",
        "MODEL_STORE_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",
    }
    if not isinstance(values, dict) or set(values) - allowed or not all(
        isinstance(value, str) for value in values.values()
    ):
        raise ValueError("invalid credentials JSON fields")
    os.environ.update(values)


class Executor:
    def __init__(self, args, clock=time.time, sleeper=time.sleep):
        self.args = args
        self.clock = clock
        self.sleep = sleeper
        self.store = StateStore(args.state_uri)
        self.db = Database(os.environ["AB_DATABASE_URL"])
        self.driver = Driver()

    def _validate_action(self):
        row = self.db.controller_action(self.args.action_key)
        if not row or row["experiment_id"] != self.args.experiment_id:
            raise ValueError("action was not registered by the controller")
        if row["action"] != self.args.action or row["target_weight"] != self.args.target_weight:
            raise ValueError("registered action parameters differ")
        state, etag = self.store.read()
        if etag != self.args.expected_state_etag or etag != row["expected_state_etag"]:
            raise ValueError("stale state ETag")
        if state.get("phase") != self.args.expected_phase or state["phase"] != row["expected_phase"]:
            raise ValueError("stale phase")
        if state["phase"] not in ACTION_PHASES[self.args.action]:
            raise ValueError("action is invalid for current phase")
        if self.args.action == "route":
            expected_weight = {"OFFLINE_PASS": 10, "CANARY": 50, "AB": 100}.get(
                state["phase"]
            )
            if self.args.target_weight != expected_weight:
                raise ValueError("route weight is invalid for current phase")
        elif self.args.target_weight is not None:
            raise ValueError("target weight is only valid for route")
        if row["status"] not in {"INTENT", "SUBMITTED", "RUNNING"}:
            raise ValueError("action is already terminal")
        self.db.update_controller_action(
            self.args.action_key, "RUNNING",
            build_number=int(os.environ["BUILD_NUMBER"]) if os.environ.get("BUILD_NUMBER") else None,
            build_url=os.environ.get("BUILD_URL"),
        )
        return state

    def prepare(self):
        if not self.args.candidate or not self.args.mode:
            raise ValueError("prepare requires candidate and experiment mode")
        engine = Engine(self.store, self.driver, self.clock)
        engine.start(
            load(self.args.candidate), self.args.mode, load(self.args.policy),
            load(self.args.fixtures), self.args.experiment_id,
        )
        engine.save(build_url=os.environ.get("BUILD_URL", ""))
        self.driver.deploy(engine.state["baseline"])
        self.driver.deploy(engine.state["pending"])
        self.driver.verify_release(engine.state["baseline"])
        self.driver.verify_release(engine.state["pending"])
        engine.event("OFFLINE", stage_started=self.clock())
        deadline = self.clock() + engine.state["policy"]["stage_timeout_seconds"]
        while self.clock() < deadline:
            evidence = self.driver.offline(engine.state)
            engine.save(
                offline_evidence=evidence,
                gate={key: evidence[key] for key in ("verdict", "reason")},
            )
            if evidence["verdict"] == "PASS":
                engine.event("OFFLINE_PASS", stage_started=self.clock())
                return
            if evidence["verdict"] == "FAIL":
                engine.event("PREPARE_FAILED")
                raise RuntimeError("offline compatibility failed")
            self.sleep(5)
        engine.event("PREPARE_FAILED", gate={"verdict": "FAIL", "reason": "offline timeout"})
        raise TimeoutError("offline evidence timeout")

    def route(self):
        weight = self.args.target_weight
        if weight not in ROUTE_PHASE:
            raise ValueError("route weight must be 10, 50 or 100")
        engine = Engine(self.store, self.driver, self.clock)
        target_phase = ROUTE_PHASE[weight]
        engine.event(
            "ROUTING", route_intent={"weight": weight, "next_phase": target_phase},
            stage_started=self.clock(),
        )
        revision = self.driver.route(engine.state, weight)
        deadline = time.monotonic() + 120
        while not self.driver.verify_route(engine.state, weight, revision):
            if time.monotonic() >= deadline:
                raise TimeoutError("Envoy propagation timeout")
            self.sleep(2)
        engine.event(
            target_phase, route_revision=revision, verified_weight=weight,
            stage_started=self.clock(),
        )

    def promote(self):
        engine = Engine(self.store, self.driver, self.clock)
        if engine.state.get("verified_weight") != 100:
            raise ValueError("candidate route is not verified at 100%")
        self.driver.verify_release(engine.state["pending"])
        if not self.driver.verify_route(engine.state, 100, engine.state["route_revision"]):
            raise ValueError("route drift before promotion")
        engine.event(
            "COMPLETED", champion=engine.state["pending"],
            previous=engine.state["baseline"], promoted_at=self.clock(),
        )

    def rollback(self):
        engine = Engine(self.store, self.driver, self.clock)
        reason = self.args.reason or "controller requested rollback"
        deadline = time.monotonic() + 125
        while engine.state.get("phase") not in {"ROLLED_BACK", "ROLLBACK_FAILED"}:
            engine.rollback(reason)
            if engine.state.get("phase") in {"ROLLED_BACK", "ROLLBACK_FAILED"}:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("rollback verification did not terminate")
            self.sleep(2)
        if engine.state["phase"] == "ROLLBACK_FAILED":
            raise RuntimeError("rollback verification failed")

    def cleanup(self):
        report = TerminalCleanup(self.store, self.driver, self.clock).run()
        if report["status"] != "CLEANED":
            raise RuntimeError("terminal cleanup did not complete")

    def run(self):
        self._validate_action()
        getattr(self, self.args.action)()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Recommendation A/B Jenkins action executor")
    result.add_argument("action", choices=sorted(ACTION_PHASES))
    result.add_argument("--action-key", required=True)
    result.add_argument("--experiment-id", required=True)
    result.add_argument("--expected-phase", required=True)
    result.add_argument("--expected-state-etag", required=True)
    result.add_argument("--target-weight", type=int)
    result.add_argument("--candidate")
    result.add_argument("--mode", choices=["config_only", "llm_only", "combined"])
    result.add_argument("--state-uri", default=os.environ.get(
        "AB_STATE_URI", "s3://recsys-llm-ab/recommendation/state.json"
    ))
    result.add_argument("--policy", default="configs/llm-ab/recommendation-live-test-policy.json")
    result.add_argument("--fixtures", default="configs/llm-ab/cases.json")
    result.add_argument("--reason")
    return result


def main() -> int:
    load_environment()
    args = parser().parse_args()
    output = Path(".llm-agent-cd")
    output.mkdir(exist_ok=True)
    try:
        Executor(args).run()
        status = "SUCCEEDED"
        reason = None
    except Exception as exc:
        status = "FAILED"
        reason = type(exc).__name__
        try:
            Database(os.environ["AB_DATABASE_URL"]).update_controller_action(
                args.action_key, status, reason=reason,
                build_number=int(os.environ["BUILD_NUMBER"]) if os.environ.get("BUILD_NUMBER") else None,
                build_url=os.environ.get("BUILD_URL"),
            )
        finally:
            raise
    else:
        Database(os.environ["AB_DATABASE_URL"]).update_controller_action(
            args.action_key, status,
            build_number=int(os.environ["BUILD_NUMBER"]) if os.environ.get("BUILD_NUMBER") else None,
            build_url=os.environ.get("BUILD_URL"),
        )
    finally:
        state, _ = StateStore(args.state_uri).read()
        evidence = {
            "event_id": digest([args.action_key, "executor-evidence"]),
            "action_key": args.action_key, "experiment_id": args.experiment_id,
            "action": args.action, "target_weight": args.target_weight,
            "build_url": os.environ.get("BUILD_URL", ""),
            "status": status if "status" in locals() else "FAILED",
            "reason": reason if "reason" in locals() else "executor initialization failed",
            "state_phase": state.get("phase"), "state_checksum": digest(state),
        }
        (output / "action.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        (output / "status.json").write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"event": "ab.action", **evidence}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
