"""Restartable single-tick state machine; Jenkins supplies scheduling and the lock."""

from __future__ import annotations

from copy import deepcopy
import json
import re

from .gates import case_gate, production_gate
from .release import digest, policy, release, validate_experiment

TERMINAL = {"COMPLETED", "ROLLED_BACK", "ROLLBACK_FAILED"}


class Engine:
    def __init__(self, store, driver, clock):
        self.store, self.driver, self.clock = store, driver, clock
        self.state, self.etag = store.read()

    def save(self, **fields):
        self.state.update(fields)
        self.etag = self.store.write(self.state, self.etag)

    def event(self, phase, **fields):
        now = self.clock()
        self.state.setdefault("events", []).append(
            {"at": now, "phase": phase, **fields}
        )
        self.save(phase=phase, **fields)
        print(json.dumps({"event": "ab.phase", "experiment_id": self.state.get("experiment_id", "baseline"),
                          "phase": phase, "at": now, "build_url": self.state.get("build_url", ""),
                          "gate": self.state.get("gate", {}), "route_revision": self.state.get("route_revision")}), flush=True)

    def start(self, candidate, mode, rules, fixtures, experiment_id):
        if experiment_id and experiment_id == self.state.get("experiment_id"):
            if (release(candidate) != self.state.get("pending") or mode != self.state.get("mode")
                    or digest(policy(rules)) != self.state.get("policy_checksum")
                    or digest(fixtures) != self.state.get("fixture_checksum")):
                raise ValueError("experiment ID reused with different inputs")
            return  # Dispatch delivery may repeat; model calls never do.
        if self.state["phase"] not in {"COMPLETED", "ROLLED_BACK", "IDLE"}:
            raise ValueError("unfinished experiment; use resume or rollback")
        if self.state.get("cleanup") and self.state["cleanup"].get("status") != "CLEANED":
            raise ValueError("unfinished terminal cleanup; run cleanup before a new experiment")
        rules = policy(rules)
        candidate = release(candidate)
        champion = release(self.state["champion"])
        if rules.get("compatibility_suite"):
            prefix = "wf" if candidate.get("scope") == "workflow" else "rec"
            if not re.fullmatch(prefix + r"-[0-9a-f]{32}", experiment_id or ""):
                raise ValueError(
                    "experiment_id must match the finite compatibility Job identity"
                )
        compatibility_fixtures = []
        compatibility_fixture_checksum = None
        compatibility_tools = []
        compatibility_tools_checksum = None
        if rules.get("compatibility_suite"):
            # Freeze the exact offline inputs in authoritative state. The
            # finite Job consumes this snapshot instead of rebuilding a suite
            # from whichever evaluator image happens to be deployed later.
            from .small_compatibility import smoke_fixtures, smoke_tools
            from .workflow_contract import SAFETY

            compatibility_contract = (
                champion["agent"]["systemMessage"]
                if champion.get("scope", "recommendation") == "recommendation"
                else SAFETY
            )
            compatibility_fixtures = smoke_fixtures(
                compatibility_contract, rules["compatibility_suite"]
            )
            compatibility_fixture_checksum = digest(compatibility_fixtures)
            compatibility_tools = smoke_tools(rules["compatibility_suite"])
            compatibility_tools_checksum = digest(compatibility_tools)
        import os
        if os.environ.get("AB_EXPECTED_BASELINE") and os.environ["AB_EXPECTED_BASELINE"] != champion["release_id"]:
            raise ValueError("STALE_BASELINE: explicit new candidate required")
        validate_experiment(champion, candidate, mode)
        candidate_a2a_preflight = None
        if (
            candidate.get("scope") == "workflow"
            and candidate.get("change_scope") == "coordinator"
        ):
            # Direct 3x2 fake-tool compatibility is necessary but insufficient
            # for a native Coordinator. Require the real zero-weight A2A gate
            # before creating experiment state or permitting a canary.
            from .runtime_probe import verified_candidate_a2a_evidence

            candidate_a2a_preflight = verified_candidate_a2a_evidence(
                self.store, candidate
            )
        if len(fixtures) != 20 or len({c["id"] for c in fixtures}) != 20:
            raise ValueError("exactly 20 unique cases required")
        disabled = list(self.state.get("disabled", []))
        if candidate["release_id"] in disabled:
            if candidate.get("scope", "recommendation") != "recommendation":
                raise ValueError("previously failed release is quarantined")
            # Recommendation retries are safe because prepare always redeploys
            # at zero traffic and reruns the complete offline gate. ``disabled``
            # is a routing/cleanup state, not a permanent model blacklist.
            disabled.remove(candidate["release_id"])
        if not experiment_id or experiment_id in self.state.get("experiment_ids", []):
            raise ValueError("experiment_id must be unique")
        self.driver.preflight(champion, candidate, fixtures)
        # MinIO versioning preserves old state revisions, while an immutable
        # snapshot gives dashboard/status readers a stable experiment lookup.
        history = list(self.state.get("history", []))
        if champion.get("scope") == "workflow" and self.state.get("experiment_id"):
            archived = self.store.archive(self.state)
            if archived not in history:
                history.append(archived)
        self.state["events"] = []
        self.event(
            "DEPLOY",
            baseline=champion,
            pending=candidate,
            mode=mode,
            experiment_id=experiment_id,
            experiment_started=self.clock(),
            policy=rules,
            policy_checksum=digest(rules),
            fixtures=fixtures,
            fixture_checksum=digest(fixtures),
            compatibility_fixtures=compatibility_fixtures,
            compatibility_fixture_checksum=compatibility_fixture_checksum,
            compatibility_tools=compatibility_tools,
            compatibility_tools_checksum=compatibility_tools_checksum,
            candidate_a2a_preflight=candidate_a2a_preflight,
            cases={},
            history=history,
            gate_evidence={},
            offline_evidence={},
            gate_windows=[],
            observation={},
            live_load_dispatched=False,
            build_url="",
            promoted_at=None,
            latency_limit=None,
            restored_baseline=False,
            cleanup={},
            route_intent={"weight": 0, "next_phase": "DEPLOY"},
            verified_weight=None,
            releases={
                **self.state.get("releases", {}),
                champion["release_id"]: champion,
                candidate["release_id"]: candidate,
            },
            disabled=disabled,
            experiment_ids=[*self.state.get("experiment_ids", []), experiment_id],
            route_snapshot=deepcopy(self.driver.snapshot_route()),
            stage_started=self.clock(),
            gate={"verdict": "HOLD", "reason": "deploying"},
        )

    def shift(self, weight, next_phase):
        # Intent persists before any K8s write. Resume repeats only idempotent routing.
        self.event(
            "ROUTING",
            route_intent={"weight": weight, "next_phase": next_phase},
            stage_started=self.clock(),
        )

    def rollback(self, reason):
        target = self.state.get("baseline")
        candidate = self.state.get("pending")
        if not target or not candidate:
            raise ValueError("no experiment snapshot to roll back")
        if target["release_id"] == candidate["release_id"]:
            raise ValueError(
                "no distinct challenger to quarantine; champion must remain enabled"
            )
        disabled = sorted(
            set(self.state.get("disabled", [])) | {candidate["release_id"]}
        )
        if self.state["phase"] != "ROLLING_BACK":
            self.event(
                "ROLLING_BACK",
                disabled=disabled,
                rollback_reason=reason,
                rollback_started=self.clock(),
            )
        # Do not catch CAS failures here: losing ownership must stop all mutations.
        try:
            revision = self.driver.route(self.state, 0)
            if not self.driver.verify_route(self.state, 0, revision):
                raise RuntimeError(
                    "rollback route not acknowledged by every ready gateway"
                )
            self.driver.verify_release(target)
        except Exception as exc:
            if self.clock() - self.state["rollback_started"] >= 120:
                self.event("ROLLBACK_FAILED", rollback_error=type(exc).__name__)
            else:
                self.save(rollback_error=type(exc).__name__)
            return
        self.event(
            "ROLLED_BACK",
            champion=target,
            route_revision=revision,
            route_intent={"weight": 0, "next_phase": "ROLLED_BACK"},
            verified_weight=0,
            gate={"verdict": "FAIL", "reason": reason},
        )

    def tick(self):
        s, now = self.state, self.clock()
        phase = s["phase"]
        if phase in TERMINAL | {"IDLE"}:
            return phase
        if (
            digest(s["policy"]) != s["policy_checksum"]
            or digest(s["fixtures"]) != s["fixture_checksum"]
        ):
            raise ValueError("persisted experiment inputs changed")
        if phase == "RESTORING":
            self.restore_baseline()
        elif phase == "ROLLING_BACK":
            self.rollback(s["rollback_reason"])
        elif phase == "DEPLOY":
            self.driver.deploy(s["baseline"])
            self.driver.deploy(s["pending"])
            self.driver.verify_release(s["baseline"])
            self.driver.verify_release(s["pending"])
            if s['policy'].get('compatibility_suite'):
                self.event('OFFLINE',stage_started=now)
            else:
                self.shift(10, "CANARY")
        elif phase == 'OFFLINE':
            evidence = self.driver.offline(s)
            self.save(offline_evidence=evidence,gate={k:evidence[k] for k in ('verdict','reason')})
            if evidence['verdict']=='FAIL':
                self.rollback('offline compatibility smoke failed')
            elif evidence['verdict']=='PASS':
                self.shift(10,'CANARY')
            elif now-s['stage_started']>=s['policy']['stage_timeout_seconds']:
                self.rollback('offline evidence timeout; no inference replay')
        elif phase == "ROUTING":
            intent = s["route_intent"]
            revision = self.driver.route(s, intent["weight"])
            if self.driver.verify_route(s, intent["weight"], revision):
                self.event(intent["next_phase"], route_revision=revision,
                           verified_weight=intent["weight"], stage_started=now)
            elif now - s["stage_started"] >= s["policy"]["stage_timeout_seconds"]:
                self.rollback("Envoy propagation timeout")
        elif phase in {"CANARY", "AB", "VERIFY"}:
            if now - s["stage_started"] >= s["policy"]["stage_timeout_seconds"]:
                self.rollback("stage timed out; insufficient evidence or samples")
                return s["phase"]
            if s['policy'].get('sample_source')=='live_test':
                self.driver.live_load(s)
                if not s.get('live_load_dispatched'):
                    self.save(live_load_dispatched=True)
            if phase == "AB":
                external_evidence = None
                # Persist STARTED first, so a lost response/restart cannot issue it twice.
                # The two-concurrent live load and the exact synthetic suite use
                # the same stock Substrate pool. Drain live work, then execute
                # one synthetic root at a time; the load Job resumes only after
                # all 20 roots finish. This is scheduling, never inference retry.
                live_inflight = (
                    self.driver.source_inflight(s, "live_test")
                    if s["policy"].get("sample_source") == "live_test"
                    else 0
                )
                synthetic_inflight = self.driver.source_inflight(s, "synthetic")
                if live_inflight == 0 and synthetic_inflight == 0:
                    if s["policy"].get("synthetic_entrypoint", "internal") == "public_a2a":
                        # Freeze all request identities before the external
                        # runner claims the durable one-shot suite.
                        changed = False
                        for case in s["fixtures"]:
                            if case["id"] not in s["cases"]:
                                s["cases"][case["id"]] = {
                                    "request_id": digest([s["experiment_id"], case["id"]]),
                                    "verdict": "STARTED",
                                }
                                changed = True
                        if changed:
                            self.save()
                        external_evidence = self.driver.send_external_suite(s)
                    else:
                        for case in s["fixtures"]:
                            row = s["cases"].get(case["id"])
                            if row is None:
                                request_id = digest([s["experiment_id"], case["id"]])
                                s["cases"][case["id"]] = {
                                    "request_id": request_id,
                                    "verdict": "STARTED",
                                }
                                self.save()
                                self.driver.send_case(s, case, request_id)
                                break  # At most one model invocation per tick.
                for case_id, row in s["cases"].items():
                    if row["verdict"] == "STARTED" or (s["policy"].get("evaluation_version")
                            and row.get("evaluation", {}).get("synced") is not True):
                        result = self.driver.case_result(row["request_id"])
                        if result is not None:
                            row.update(result)
                            print(json.dumps({"event": "ab.case", "experiment_id": s["experiment_id"],
                                              "case_id": case_id, **result}), flush=True)
                self.save()
            observation = self.driver.observe(s, s["stage_started"], now)
            verdict, reason = production_gate(
                observation,
                s["policy"],
                compare=phase == "AB",
                champion_required=phase != "VERIFY",
            )
            if phase == "AB":
                cv, cr = case_gate(
                    s["cases"], s["baseline"]["release_id"], s["pending"]["release_id"],
                    s["policy"].get("evaluation_version")
                )
                if s["policy"].get("synthetic_entrypoint", "internal") == "public_a2a":
                    external_evidence = external_evidence or self.driver.external_suite_evidence(s)
                    ticket_counts = external_evidence.get("tickets", {})
                    if cv != "FAIL" and (
                        not external_evidence.get("run")
                        or sum(ticket_counts.values()) != 20
                        or ticket_counts.get("COMPLETED", 0) != 20
                        or external_evidence["run"].get("status") != "COMPLETED"
                    ):
                        cv, cr = "HOLD", "public A2A ticket suite is incomplete or ambiguous"
                if cv == "FAIL" or verdict == "PASS" and cv != "PASS":
                    verdict, reason = cv, cr
            self.save(
                gate={"verdict": verdict, "reason": reason}, observation=observation,
                gate_evidence={"start": s["stage_started"], "end": now, "phase": phase,
                               "source": s["policy"].get("sample_source", "production"),
                               "observation": observation, "latency_ratio": s["policy"]["latency_ratio"],
                               "verdict": verdict, "reason": reason,
                               "evaluation": {"version":s['policy'].get('evaluation_version'),
                                   "fixture_checksum":s['fixture_checksum'],
                                   "cases_checksum":digest(s.get('cases',{})),
                                   "confirmed":sum(bool(r.get('evaluation',{}).get('synced')) for r in s.get('cases',{}).values()),
                                   "evidence":{k:r.get('evaluation_evidence',{}).get('evidence_checksum') for k,r in s.get('cases',{}).items()}}}
            )
            if verdict == "FAIL":
                self.save(gate_windows=[*s.get("gate_windows", []), deepcopy(s["gate_evidence"])])
                self.rollback(reason)
            elif (
                verdict == "PASS"
                and now - s["stage_started"] >= s["policy"]["window_seconds"]
            ):
                self.save(gate_windows=[*s.get("gate_windows", []), deepcopy(s["gate_evidence"])])
                if phase == "CANARY":
                    self.shift(50, "AB")
                elif phase == "AB":
                    self.save(
                        latency_limit=observation["champion"]["p95"]
                        * s["policy"]["latency_ratio"]
                    )
                    self.shift(100, "VERIFY")
                else:
                    if s["pending"].get("scope") == "workflow":
                        self.driver.verify_release(s["pending"])
                        if not self.driver.verify_route(s, 100, s["route_revision"]):
                            self.rollback("route drift before workflow promotion")
                            return s["phase"]
                    self.event(
                        "MONITOR" if s["policy"]["monitor_seconds"] else "COMPLETED",
                        champion=s["pending"],
                        previous=s["baseline"],
                        promoted_at=now,
                        last_monitor=now,
                        bad_windows=0,
                        telemetry_failures=0,
                    )
        elif phase == "MONITOR":
            if now - s["last_monitor"] < s["policy"]["monitor_interval_seconds"]:
                return phase
            observation = self.driver.observe(s, s["last_monitor"], now)
            c = observation.get("candidate", {})
            missing = not observation.get("healthy") or c.get("unknown", 0) > 0
            bad = c.get("count", 0) >= s["policy"]["min_samples"] and (
                c.get("errors", 0) > 0 or (c.get("p95") or 0) > s["latency_limit"]
            )
            self.save(
                last_monitor=now,
                observation=observation,
                telemetry_failures=s["telemetry_failures"] + 1 if missing else 0,
                bad_windows=s["bad_windows"] + 1 if bad else 0,
            )
            if (
                c.get("contract_failures", 0)
                or s["bad_windows"] >= 2
                or s["telemetry_failures"] >= 2
            ):
                self.rollback("post-promotion monitor violation")
            elif now - s["promoted_at"] >= s["policy"]["monitor_seconds"]:
                self.event("COMPLETED")
        else:
            raise ValueError(f"unknown phase: {phase}")
        return self.state["phase"]

    def restore_baseline(self):
        s = self.state
        if s["phase"] not in {"COMPLETED", "RESTORING"} or s["policy"].get("sample_source") != "live_test":
            raise ValueError("baseline restore is restricted to completed live-test acceptance")
        if s["phase"] != "RESTORING":
            self.event("RESTORING", restore_from=s["champion"]["release_id"])
        self.driver.verify_release(s["baseline"])
        revision = self.driver.route(s, 0)
        if not self.driver.verify_route(s, 0, revision):
            raise ValueError("baseline restore not acknowledged; resume only")
        self.event("COMPLETED", champion=s["baseline"], previous=s["pending"],
                   verified_weight=0, route_revision=revision,
                   route_intent={"weight": 0, "next_phase": "COMPLETED"},
                   restored_baseline=True)
