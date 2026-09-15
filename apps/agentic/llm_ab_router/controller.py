"""Single-tick Recommendation rollout controller.

Only this component evaluates evidence and chooses the next Jenkins action.
Jenkins remains a mutation executor; the Traffic Job remains a traffic source.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import os
import time
from urllib.parse import quote

import httpx

from jenkins.python.llm_agent_cd.gates import case_gate, production_gate
from jenkins.python.llm_agent_cd.release import digest
from jenkins.python.llm_agent_cd.state import StateStore

ACTION_TERMINAL = {"SUCCEEDED", "FAILED", "NEEDS_ATTENTION"}
STATE_TERMINAL = {"COMPLETED", "ROLLED_BACK", "ROLLBACK_FAILED"}


def json_evidence(value):
    """Normalize database evidence without weakening missing-data semantics."""
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, dict):
        return {key: json_evidence(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_evidence(item) for item in value]
    return value


def epoch_seconds(value):
    return value.timestamp() if isinstance(value, datetime) else float(value)


def gate_decision(phase, verdict, elapsed, window_seconds, timeout_seconds, *, pending_live=False):
    """Pure transition policy used by the reconcile loop and unit tests."""
    if phase not in {"CANARY", "AB", "VERIFY"}:
        raise ValueError("gate decision requires an observation phase")
    if verdict == "FAIL" or elapsed >= timeout_seconds:
        return "rollback", None
    if verdict != "PASS" or elapsed < window_seconds or pending_live:
        return None, None
    if phase == "CANARY":
        return "route", 50
    if phase == "AB":
        return "route", 100
    return "promote", None


class RecommendationController:
    def __init__(self, trigger, clock=time.time):
        self.trigger = trigger
        self.db = trigger.db
        self.clock = clock
        from .trigger import state_uri

        self.store = StateStore(state_uri("recommendation"))

    @staticmethod
    def action_key(experiment_id, action, target_weight, phase, etag):
        return digest([experiment_id, action, target_weight, phase, etag])

    @staticmethod
    def _parameters(row, request):
        from .trigger import jenkins_parameters, state_uri

        params = {
            "ACTION": row["action"],
            "ACTION_KEY": row["action_key"],
            "EXPERIMENT_ID": row["experiment_id"],
            "EXPECTED_PHASE": row["expected_phase"],
            "EXPECTED_STATE_ETAG": row["expected_state_etag"],
            "TARGET_WEIGHT": "" if row["target_weight"] is None else str(row["target_weight"]),
            "STATE_URI": state_uri("recommendation"),
            "SOURCE_MODE": "deployed-image",
            "ROUTER_IMAGE": os.environ.get("AB_RECOMMENDATION_ROUTER_IMAGE")
            or os.environ["AB_DISPATCH_IMAGE"],
        }
        if row["action"] == "prepare":
            params.update(jenkins_parameters(
                "recommendation", request["config"], request["candidate_uri"],
                state_uri("recommendation"),
            ))
            params["ACTION"] = "prepare"
            params["ACTION_KEY"] = row["action_key"]
            params["EXPECTED_PHASE"] = row["expected_phase"]
            params["EXPECTED_STATE_ETAG"] = row["expected_state_etag"]
            params["TARGET_WEIGHT"] = ""
        return params

    @staticmethod
    def _matches(entry, action_key):
        return any(
            parameter.get("name") == "ACTION_KEY" and parameter.get("value") == action_key
            for action in entry.get("actions", [])
            for parameter in action.get("parameters", [])
        )

    def reconcile_action(self, row):
        """Project Jenkins delivery; never submit from reconciliation."""
        from .trigger import jenkins_job

        job_name = jenkins_job("recommendation")
        job = quote(job_name, safe="")
        tree = "builds[number,url,building,result,actions[parameters[name,value]]]{0,100}"
        builds = self.trigger.jenkins("/job/" + job + "/api/json", params={"tree": tree}).json().get("builds", [])
        queue = self.trigger.jenkins(
            "/queue/api/json",
            params={"tree": "items[id,task[name],actions[parameters[name,value]]]"},
        ).json().get("items", [])
        found = [item for item in builds if self._matches(item, row["action_key"])]
        waiting = [item for item in queue if item.get("task", {}).get("name") == job_name
                   and self._matches(item, row["action_key"])]
        if len(found) + len(waiting) > 1:
            return self.db.update_controller_action(
                row["action_key"], "NEEDS_ATTENTION",
                reason="multiple Jenkins deliveries found",
            )
        if found:
            build = found[0]
            values = {"build_number": build["number"], "build_url": build["url"]}
            current = self.db.controller_action(row["action_key"])
            if build.get("building"):
                if current["status"] not in ACTION_TERMINAL:
                    return self.db.update_controller_action(row["action_key"], "RUNNING", **values)
                return current
            if current["status"] in ACTION_TERMINAL:
                return current
            if build.get("result") == "SUCCESS":
                return self.db.update_controller_action(
                    row["action_key"], "NEEDS_ATTENTION", **values,
                    reason="Jenkins succeeded without executor evidence",
                )
            return self.db.update_controller_action(
                row["action_key"], "FAILED", **values,
                reason="Jenkins result " + str(build.get("result")),
            )
        if waiting:
            return self.db.update_controller_action(
                row["action_key"], "SUBMITTED", queue_id=str(waiting[0]["id"]),
            )
        if row["status"] == "INTENT" and self.clock() - row["created_at"].timestamp() >= 60:
            return self.db.update_controller_action(
                row["action_key"], "NEEDS_ATTENTION",
                reason="uncertain Jenkins delivery; no automatic resubmit",
            )
        return row

    def reconcile_actions(self):
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM recsys_ab.controller_actions
                   WHERE status IN ('INTENT','SUBMITTED','RUNNING')
                   ORDER BY created_at LIMIT 20"""
            ).fetchall()
        return [self.reconcile_action(dict(row)) for row in rows]

    def dispatch(self, experiment_id, action, *, target_weight=None, reason=None):
        """Register and submit at most one immutable action."""
        from .trigger import jenkins_job

        state, etag = self.store.read()
        key = self.action_key(experiment_id, action, target_weight, state["phase"], etag)
        intent = {
            "action_key": key, "experiment_id": experiment_id, "action": action,
            "target_weight": target_weight, "expected_phase": state["phase"],
            "expected_state_etag": etag,
        }
        row, created = self.db.create_controller_action(intent)
        if not created:
            return self.reconcile_action(row)
        request = self.trigger.request(experiment_id)
        if not request:
            return self.db.update_controller_action(key, "FAILED", reason="trigger request missing")
        params = self._parameters(row, request)
        if reason:
            params["REASON"] = reason[:160]
        job = quote(jenkins_job("recommendation"), safe="")
        try:
            response = self.trigger.jenkins(
                "/job/" + job + "/buildWithParameters", method="POST", data=params,
            )
        except httpx.HTTPError as exc:
            self.db.update_controller_action(
                key, "INTENT", reason="uncertain Jenkins response: " + type(exc).__name__,
            )
            return self.db.controller_action(key)
        return self.db.update_controller_action(
            key, "SUBMITTED",
            queue_id=response.headers.get("location", "").rstrip("/").split("/")[-1] or None,
        )

    def _save(self, state, etag, **fields):
        state.update(fields)
        return self.store.write(state, etag)

    def _prometheus_fresh(self):
        base = os.environ.get("AB_PROMETHEUS_URL")
        if not base:
            return True
        try:
            response = self.trigger.http.get(
                base.rstrip("/") + "/api/v1/query",
                params={"query": "max(recsys_workflow_telemetry_timestamp)"},
            )
            response.raise_for_status()
            result = response.json().get("data", {}).get("result", [])
            return bool(result) and self.clock() - float(result[0]["value"][1]) <= 120
        except (httpx.HTTPError, ValueError, KeyError, IndexError):
            return False

    def _refresh_cases(self, state):
        changed = False
        for case in state.get("fixtures", []):
            row = state.setdefault("cases", {}).setdefault(
                case["id"],
                {"request_id": digest([state["experiment_id"], case["id"]]),
                 "verdict": "STARTED"},
            )
            result = self.db.result(row["request_id"])
            if result is not None and result != {key: row.get(key) for key in result}:
                row.update(result)
                changed = True
        return changed

    def _pending_live(self, experiment_id):
        evidence = self.db.traffic_evidence(experiment_id)
        counts = evidence["live_tickets"]
        return sum(counts.get(status, 0) for status in ("DISPATCH_INTENT", "CLAIMED"))

    def _gate(self, state):
        now = self.clock()
        phase = state["phase"]
        observation = self.db.observe(state, state["stage_started"], now)
        if not self._prometheus_fresh():
            observation["healthy"] = False
        if state["policy"].get("sample_source") == "live_test":
            observation["organic"] = self.db.observe(
                state, state["stage_started"], now, source="production"
            )
        verdict, reason = production_gate(
            observation, state["policy"], compare=phase == "AB",
            champion_required=phase != "VERIFY",
        )
        traffic = json_evidence(self.db.traffic_evidence(state["experiment_id"]))
        run = traffic["run"]
        if not run and verdict != "FAIL":
            verdict, reason = "HOLD", "awaiting traffic"
        elif run["status"] in {"FAILED", "LOST", "NEEDS_ATTENTION"}:
            verdict, reason = "FAIL", "Traffic Job " + run["status"].lower()
        elif run.get("heartbeat_at") and now - epoch_seconds(run["heartbeat_at"]) > 120:
            verdict, reason = "HOLD", "Traffic Job heartbeat is stale"
        elif traffic["live_tickets"].get("REJECTED", 0):
            verdict, reason = "FAIL", "Traffic Job has rejected public requests"
        elif traffic["live_tickets"].get("AMBIGUOUS", 0) and verdict != "FAIL":
            verdict, reason = "HOLD", "Traffic Job has ambiguous public responses"
        if phase == "AB":
            self._refresh_cases(state)
            case_verdict, case_reason = case_gate(
                state.get("cases", {}), state["baseline"]["release_id"],
                state["pending"]["release_id"], state["policy"].get("evaluation_version"),
            )
            external = traffic["external"]
            ticket_counts = external.get("tickets", {})
            if case_verdict != "FAIL" and (
                not external.get("run") or sum(ticket_counts.values()) != 20
                or ticket_counts.get("COMPLETED", 0) != 20
                or external["run"].get("status") != "COMPLETED"
            ):
                case_verdict, case_reason = "HOLD", "public A2A 20-case suite incomplete"
            if case_verdict == "FAIL" or verdict == "PASS" and case_verdict != "PASS":
                verdict, reason = case_verdict, case_reason
        evidence = {
            "start": state["stage_started"], "end": now, "phase": phase,
            "source": state["policy"].get("sample_source", "production"),
            "observation": observation, "traffic": traffic,
            "latency_ratio": state["policy"]["latency_ratio"],
            "verdict": verdict, "reason": reason,
            "cases_checksum": digest(state.get("cases", {})),
        }
        return verdict, reason, evidence, observation

    def _record_gate(self, state, etag, verdict, reason, evidence, observation):
        state["gate"] = {"verdict": verdict, "reason": reason}
        state["gate_evidence"] = evidence
        state["observation"] = observation
        return self._save(state, etag)

    def _close_window(self, state):
        windows = state.setdefault("gate_windows", [])
        phase = state["phase"]
        if not any(row.get("phase") == phase and row.get("end") == state["gate_evidence"]["end"]
                   for row in windows):
            windows.append(deepcopy(state["gate_evidence"]))

    def tick(self, experiment_id):
        """Evaluate once and dispatch no more than one Jenkins action."""
        state, etag = self.store.read()
        request = self.trigger.request(experiment_id)
        if not request:
            return {"status": "IDLE"}
        latest = self.db.latest_controller_action(experiment_id)
        if latest and latest["status"] in {"INTENT", "SUBMITTED", "RUNNING"}:
            return {"status": "HOLD", "reason": "Jenkins action in progress",
                    "action_key": latest["action_key"]}
        if latest and latest["status"] == "NEEDS_ATTENTION":
            self.trigger.update(experiment_id, "NEEDS_ATTENTION", reason=latest["reason"])
            return {"status": "NEEDS_ATTENTION", "reason": latest["reason"]}
        if state.get("experiment_id") != experiment_id:
            if latest and latest["status"] == "FAILED" and latest["action"] == "prepare":
                reason = latest["reason"] or "prepare executor failed before state ownership"
                self.trigger.update(experiment_id, "REJECTED", reason=reason)
                return {"status": "REJECTED", "reason": reason}
            if state["phase"] not in {"IDLE", "COMPLETED", "ROLLED_BACK"}:
                return {"status": "HOLD", "reason": "another experiment owns state"}
            if state.get("cleanup") and state["cleanup"].get("status") != "CLEANED":
                return {"status": "HOLD", "reason": "previous cleanup pending"}
            self.trigger.update(experiment_id, "RUNNING", reason="prepare dispatched")
            action = self.dispatch(experiment_id, "prepare")
            return {"status": action["status"], "action": "prepare"}

        phase = state["phase"]
        if latest and latest["status"] == "FAILED" and phase not in STATE_TERMINAL:
            if latest["action"] == "rollback":
                self.trigger.update(experiment_id, "ROLLBACK_FAILED", reason=latest["reason"])
                return {"status": "ROLLBACK_FAILED"}
            action = self.dispatch(experiment_id, "rollback", reason=latest["reason"] or "executor failed")
            return {"status": action["status"], "action": "rollback"}
        if phase == "OFFLINE_PASS":
            action = self.dispatch(experiment_id, "route", target_weight=10)
            return {"status": action["status"], "action": "route", "weight": 10}
        if phase in {"DEPLOY", "OFFLINE", "ROUTING", "ROLLING_BACK", "PREPARE_FAILED"}:
            if phase == "PREPARE_FAILED":
                action = self.dispatch(experiment_id, "rollback", reason="offline preparation failed")
                return {"status": action["status"], "action": "rollback"}
            return {"status": "HOLD", "reason": "executor transition incomplete"}
        if phase in {"CANARY", "AB", "VERIFY"}:
            verdict, reason, evidence, observation = self._gate(state)
            etag = self._record_gate(state, etag, verdict, reason, evidence, observation)
            elapsed = self.clock() - state["stage_started"]
            action, weight = gate_decision(
                phase, verdict, elapsed, state["policy"]["window_seconds"],
                state["policy"]["stage_timeout_seconds"],
                pending_live=self._pending_live(experiment_id) > 0,
            )
            if action == "rollback":
                self._close_window(state)
                self._save(state, etag, gate_windows=state["gate_windows"])
                dispatched = self.dispatch(
                    experiment_id, "rollback",
                    reason=reason if verdict == "FAIL" else "stage timeout: " + reason,
                )
                return {"status": dispatched["status"], "action": "rollback"}
            if action is None:
                if verdict == "PASS" and elapsed >= state["policy"]["window_seconds"]:
                    reason = "draining live requests before route change"
                self.trigger.update(experiment_id, "HOLD", reason=reason)
                return {"status": "HOLD", "reason": reason}
            self._close_window(state)
            fields = {"gate_windows": state["gate_windows"]}
            if phase == "AB":
                fields["latency_limit"] = observation["champion"]["p95"] * state["policy"]["latency_ratio"]
            etag = self._save(state, etag, **fields)
            dispatched = self.dispatch(experiment_id, action, target_weight=weight)
            return {"status": dispatched["status"], "action": action, "weight": weight}
        if phase in {"COMPLETED", "ROLLED_BACK"}:
            if state.get("cleanup", {}).get("status") != "CLEANED":
                action = self.dispatch(experiment_id, "cleanup")
                return {"status": action["status"], "action": "cleanup"}
            self.trigger.update(experiment_id, phase, reason=None)
            return {"status": phase}
        if phase == "ROLLBACK_FAILED":
            self.trigger.update(experiment_id, phase, reason="route rollback is unverified")
            return {"status": phase}
        return {"status": "HOLD", "reason": "unknown phase " + str(phase)}
