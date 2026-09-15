from copy import deepcopy
import hashlib
import hmac
import json

import pytest

from jenkins.python.llm_agent_cd.release import release, validate_experiment, policy, DEFAULT_POLICY, digest
from jenkins.python.llm_agent_cd.workflow import members, diff, role_llms, ROLES, TOKENS
from jenkins.python.llm_agent_cd.manifests import name as resource_name, resources, virtual_service
from apps.agentic.llm_ab_router.trigger import signature_valid, candidate_from_config, dispatch_job
from tests.unit.jenkins.test_llm_agent_cd import champion


@pytest.fixture
def bundle(champion):
    agents = {role: deepcopy(champion["agent"]) for role in ROLES}
    agents["coordinator"]["tools"] = [{"type": "Agent", "agent": {"name": token, "kind": "SandboxAgent", "apiGroup": "kagent.dev"}} for token in TOKENS.values()]
    agents["coordinator"]["systemMessage"] = "Call kagent__NS__{{agent.recommendation}} then kagent__NS__{{agent.context}}."
    return release({"schema_version": 1, "scope": "workflow", "global_generation": champion["config"],
        "agent_overrides": {}, "llm": champion["llm"], "agents": agents,
        "bindings": {role: deepcopy(champion["binding"]) for role in ROLES}})


def candidate(a, config=False, llm=False):
    b = {k: deepcopy(a[k]) for k in ("schema_version", "scope", "global_generation", "agent_overrides", "llm", "agents", "bindings")}
    if config:
        b["global_generation"]["temperature"] = "0.2"
    if llm:
        b["llm"]["artifact_sha256"] = "f" * 64
    return release(b)


@pytest.mark.parametrize("mode,c,l", [("config_only", True, False), ("llm_only", False, True), ("combined", True, True)])
def test_workflow_axes(bundle, mode, c, l):
    b = candidate(bundle, c, l)
    validate_experiment(bundle, b, mode)
    for wrong in {"config_only", "llm_only", "combined"} - {mode}:
        with pytest.raises(ValueError):
            validate_experiment(bundle, b, wrong)
    assert all(x["changed"] == c for x in diff(bundle, b).values())


def test_rendered_edges_are_same_bundle(bundle):
    m = members(bundle)
    root = m["coordinator"]
    names = [t["agent"]["name"] for t in root["agent"]["tools"]]
    assert set(names) == {"rec-ab-" + m[role]["release_id"][:20] for role in ROLES[1:]}
    assert "{{agent." not in root["agent"]["systemMessage"]
    assert all(n.replace("-", "_") in root["agent"]["systemMessage"] for n in names)
    objs = resources(bundle, "kagent", "img@sha256:" + "a" * 64, "secret")
    assert sum(x["kind"] == "SandboxAgent" for x in objs) == 3
    assert sum(x["kind"] == "ModelConfig" for x in objs) == 3
    assert sum(x["kind"] == "Deployment" for x in objs) == 2


def test_override_noop_and_tamper(bundle):
    raw = {k: deepcopy(bundle[k]) for k in ("schema_version", "scope", "global_generation", "agent_overrides", "llm", "agents", "bindings")}
    raw["agent_overrides"] = {role: {"temperature": "0.7"} for role in ROLES}
    a = release(raw)
    raw["global_generation"]["temperature"] = "0.4"
    b = release(raw)
    assert a["config_id"] == b["config_id"]
    with pytest.raises(ValueError, match="NOOP"):
        validate_experiment(a, b, "config_only")
    raw = deepcopy(bundle)
    raw["global_generation"]["temperature"] = "0.3"
    with pytest.raises(ValueError, match="checksum"):
        release(raw)


def test_workflow_route_is_separate(bundle):
    route = virtual_service({"baseline": bundle, "pending": candidate(bundle, True)}, 50, "kagent")
    assert route["metadata"]["name"] == "recsys-workflow-ab"
    assert route["spec"]["gateways"] == ["recsys-workflow-gateway"]
    assert all("mirror" not in x and x.get("retries", {}).get("attempts", 0) == 0 for x in route["spec"]["http"])


def test_policy_can_disable_monitor():
    assert policy({**DEFAULT_POLICY, "monitor_seconds": 0, "sample_source": "live_test"})["monitor_seconds"] == 0
    assert policy({**DEFAULT_POLICY, "monitor_seconds": 0,
                   "compatibility_suite": "recommendation-compatibility-smoke-v1"})["compatibility_suite"] == "recommendation-compatibility-smoke-v1"
    assert policy({**DEFAULT_POLICY, "monitor_seconds": 0,
                   "compatibility_suite": "recommendation-compatibility-smoke-v2"})["compatibility_suite"] == "recommendation-compatibility-smoke-v2"
    for field in ("window_seconds", "request_timeout_seconds", "min_samples"):
        with pytest.raises(ValueError):
            policy({**DEFAULT_POLICY, field: 0})
    with pytest.raises(ValueError):
        policy({**DEFAULT_POLICY, "sample_source": "synthetic"})


def test_signature():
    raw = b'{"id":"event"}'
    sig = hmac.new(b"secret", b"123." + raw, hashlib.sha256).hexdigest()
    assert signature_valid(raw, "t=123,v1=" + sig, "secret") == (True, 123)
    assert signature_valid(raw + b" ", "t=123,v1=" + sig, "secret")[0] is False
    assert signature_valid(raw, "bad", "secret") == (False, 0)


def test_candidate_catalog_and_stale_baseline(bundle):
    config = {"schema_version": 1, "scope": "workflow", "baseline_workflow_release_id": bundle["release_id"],
              "global_generation": {**bundle["global_generation"], "temperature": "0.2"},
              "llm_release_ref": bundle["llm_version_id"], "experiment_type": "config_only", "policy_ref": "workflow-production"}
    b = candidate_from_config(bundle, config, bundle["llm"])
    assert b["bindings"] == bundle["bindings"]
    with pytest.raises(ValueError, match="STALE_BASELINE"):
        candidate_from_config(b, config, bundle["llm"])
    with pytest.raises(ValueError, match="schema"):
        candidate_from_config(bundle, {**config, "image": "evil"}, bundle["llm"])


def test_coordinator_only_llm_candidate_freezes_specialists(bundle):
    llm = deepcopy(bundle["llm"])
    llm["artifact_sha256"] = "f" * 64
    config = {
        "schema_version": 1,
        "scope": "workflow",
        "baseline_workflow_release_id": bundle["release_id"],
        "global_generation": deepcopy(bundle["global_generation"]),
        "llm_release_ref": digest(llm),
        "experiment_type": "llm_only",
        "policy_ref": "workflow-production",
        "target_role": "coordinator",
    }
    candidate = candidate_from_config(bundle, config, llm)
    validate_experiment(bundle, candidate, "llm_only")
    before, after = members(bundle), members(candidate)
    assert candidate["change_scope"] == "coordinator"
    assert role_llms(candidate)["coordinator"] == llm
    assert before["coordinator"]["llm_version_id"] != after["coordinator"]["llm_version_id"]
    for role in ("context", "recommendation"):
        assert before[role]["llm_version_id"] == after[role]["llm_version_id"]
        assert before[role]["config_id"] == after[role]["config_id"]
        assert bundle["bindings"][role] == candidate["bindings"][role]
    assert {role: row["llm_changed"] for role, row in diff(bundle, candidate).items()} == {
        "coordinator": True, "context": False, "recommendation": False
    }


def test_coordinator_only_rejects_specialist_llm_or_binding_drift(bundle):
    llm = deepcopy(bundle["llm"])
    llm["artifact_sha256"] = "f" * 64
    config = {"schema_version": 1, "scope": "workflow",
        "baseline_workflow_release_id": bundle["release_id"],
        "global_generation": deepcopy(bundle["global_generation"]),
        "llm_release_ref": digest(llm), "experiment_type": "llm_only",
        "policy_ref": "workflow-production", "target_role": "coordinator"}
    candidate = candidate_from_config(bundle, config, llm)
    raw = {k: deepcopy(candidate[k]) for k in (
        "schema_version", "scope", "global_generation", "agent_overrides", "llm",
        "llm_overrides", "change_scope", "agents", "bindings")}
    raw["llm_overrides"]["context"] = deepcopy(llm)
    with pytest.raises(ValueError, match="specialist context"):
        validate_experiment(bundle, release(raw), "llm_only")
    raw = {k: deepcopy(candidate[k]) for k in (
        "schema_version", "scope", "global_generation", "agent_overrides", "llm",
        "llm_overrides", "change_scope", "agents", "bindings")}
    raw["bindings"]["recommendation"]["backend_url"] = "http://drift"
    with pytest.raises(ValueError, match="specialist recommendation"):
        validate_experiment(bundle, release(raw), "llm_only")


def test_job_deterministic_and_no_serving_permissions(monkeypatch):
    monkeypatch.setenv("AB_DISPATCH_IMAGE", "router@sha256:" + "b" * 64)
    monkeypatch.setenv("AB_PROFILE_OVERLAY_CONFIGMAP", "recsys-workflow-trigger-code-deadbeef")
    job = dispatch_job("wf-" + "a" * 32)
    assert job == dispatch_job("wf-" + "a" * 32)
    assert job["spec"]["activeDeadlineSeconds"] == 300
    assert job["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    assert job["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"] == "recsys-workflow-trigger-code-deadbeef"
    assert len(job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]) == 4
    with pytest.raises(ValueError):
        dispatch_job("../evil")


def test_foreign_job_name_collision_is_not_idempotent_success(monkeypatch):
    from apps.agentic.llm_ab_router.trigger import dispatch_job_matches
    monkeypatch.setenv("AB_DISPATCH_IMAGE", "router@sha256:" + "b" * 64)
    desired = dispatch_job("wf-" + "a" * 32)
    existing = deepcopy(desired)
    existing["spec"]["selector"] = {"matchLabels": {"controller-uid": "kubernetes-default"}}
    assert dispatch_job_matches(existing, desired)
    existing["spec"]["template"]["spec"]["containers"][0]["image"] = "foreign"
    assert not dispatch_job_matches(existing, desired)


def test_zero_monitor_completes_and_does_not_schedule_followup(champion):
    from tests.unit.jenkins.test_llm_agent_cd import MemoryStore, FakeDriver, change, ROOT
    from jenkins.python.llm_agent_cd.engine import Engine
    store, driver, now = MemoryStore(champion), FakeDriver(), [1000]
    e = Engine(store, driver, lambda: now[0])
    e.start(change(champion, True), "config_only", {**DEFAULT_POLICY, "monitor_seconds": 0},
            json.loads((ROOT / "configs/llm-ab/cases.json").read_text()), "zero-monitor")
    e.tick()
    e.tick()
    now[0] += 600
    e.tick()
    e.tick()
    for _ in range(20):
        e.tick()
    now[0] += 600
    e.tick()
    e.tick()
    now[0] += 600
    e.tick()
    assert e.state["phase"] == "COMPLETED"
    assert e.state["previous"] == champion
    before = list(driver.weights)
    driver.o["candidate"]["errors"] = 10
    now[0] += 86400
    e.tick()
    assert e.state["phase"] == "COMPLETED" and driver.weights == before


def test_live_acceptance_never_hides_organic_error():
    from jenkins.python.llm_agent_cd.gates import production_gate
    stats = {"count": 10, "errors": 0, "contract_failures": 0, "unknown": 0, "p95": 1.0}
    observation = {"healthy": True, "champion": stats, "candidate": stats,
                   "organic": {"candidate": {"errors": 1}}}
    assert production_gate(observation, DEFAULT_POLICY, compare=True)[0] == "FAIL"


def test_live_load_job_is_not_retried():
    from jenkins.python.llm_agent_cd.live_test import job
    j = job("wf-" + "a" * 32, "router@sha256:" + "b" * 64)
    assert j["spec"]["backoffLimit"] == 0
    assert j["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    recommendation = job("rec-" + "c" * 32, "router@sha256:" + "b" * 64)
    assert recommendation["metadata"]["labels"]["app"] == "recsys-agent-live-test"
    environment = {
        item["name"]: item["value"]
        for item in recommendation["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert environment["AB_ROUTER_URL"] == (
        "http://recsys-ab-router.kagent.svc.cluster.local"
    )


def test_live_load_rejects_an_external_router():
    from jenkins.python.llm_agent_cd.live_test import job

    with pytest.raises(ValueError, match="internal trusted router"):
        job(
            "rec-" + "c" * 32,
            "router@sha256:" + "b" * 64,
            router_url="https://example.com",
        )


def test_live_load_pauses_during_exact_suite_and_resumes_after_completion():
    from jenkins.python.llm_agent_cd.live_test import load_allowed

    class DB:
        complete = False

        def synthetic_suite_complete(self, experiment_id, expected):
            assert experiment_id == "rec-" + "a" * 32
            assert expected == 20
            return self.complete

    db = DB()
    state = {
        "phase": "AB",
        "experiment_id": "rec-" + "a" * 32,
        "fixtures": [{}] * 20,
    }
    assert load_allowed(state, db) is False
    db.complete = True
    assert load_allowed(state, db) is True
    state["phase"] = "CANARY"
    db.complete = False
    assert load_allowed(state, db) is True


def test_missing_child_history_is_hold_not_replay(bundle):
    from jenkins.python.llm_agent_cd.workflow_evidence import inspect_workflow
    m = members(bundle)["recommendation"]
    name = "kagent__NS__" + resource_name(m).replace("-", "_")
    body = {"result": {"task": {"status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{"parts": [
        {"metadata": {"adk_type": "function_call"}, "data": {"id": "call1", "name": name}},
        {"metadata": {"adk_type": "function_response"}, "data": {"id": "call1", "name": name,
         "response": {"result": "LLM text is not a trace", "subagent_session_id": "child-session"}}}]}]}}}
    reads = []
    def reader(sid):
        reads.append(sid)
        raise LookupError("missing child")
    result = inspect_workflow(body, {"trajectory": ["recommendation"]}, bundle, child_reader=reader)
    assert result["verdict"] == "HOLD" and reads == ["child-session"]


def test_context_fastmcp_envelope_matches_runtime_rendered_root(bundle):
    from jenkins.python.llm_agent_cd.workflow_evidence import inspect_workflow
    context = members(bundle)["context"]
    agent = "kagent__NS__" + resource_name(context).replace("-", "_")
    business = {"chunk_id": "chunk-1", "text": "evidence"}
    wrapped = {"output": business}
    child = {"status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{"parts": [
        {"metadata": {"adk_type": "function_call"},
         "data": {"id": "inner", "name": "get_chunk_by_id",
                  "args": {"chunk_id": "chunk-1"}}},
        {"metadata": {"adk_type": "function_response"},
         "data": {"id": "inner", "name": "get_chunk_by_id",
                  "response": wrapped}},
        {"text": json.dumps(wrapped)},
    ]}]}
    root = {"status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{"parts": [
        {"metadata": {"adk_type": "function_call"},
         "data": {"id": "outer", "name": agent, "args": {"request": "chunk"}}},
        {"metadata": {"adk_type": "function_response"},
         "data": {"id": "outer", "name": agent,
                  "response": {"result": json.dumps(wrapped),
                               "subagent_session_id": "child-context"}}},
        {"text": json.dumps(business), "metadata": {"runtime_rendered": True}},
    ]}]}
    expected = {"trajectory": ["context"], "context": {
        "tool": "get_chunk_by_id", "arguments": {"chunk_id": "chunk-1"}}}
    result = inspect_workflow(
        {"result": {"task": root}}, expected, bundle,
        child_reader=lambda sid: {"result": {"task": child}})
    assert result["verdict"] == "PASS"


def test_trusted_runtime_renderer_accepts_context_tool_json_when_child_model_wrote_prose(bundle):
    from jenkins.python.llm_agent_cd.workflow_evidence import inspect_workflow
    context = members(bundle)["context"]
    agent = "kagent__NS__" + resource_name(context).replace("-", "_")
    business = {"chunk_id": "chunk-1", "text": "evidence", "source_key": "s"}
    child = {"status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{"parts": [
        {"metadata": {"adk_type": "function_call"},
         "data": {"id": "inner", "name": "get_chunk_by_id",
                  "args": {"chunk_id": "chunk-1"}}},
        {"metadata": {"adk_type": "function_response"},
         "data": {"id": "inner", "name": "get_chunk_by_id",
                  "response": {"output": business}}},
        {"text": "The chunk says evidence."},
    ]}]}
    root = {"status": {"state": "TASK_STATE_COMPLETED"},
            "metadata": {"runtime_rendered": True,
                         "runtime_output_profile": "trusted-child-tool-results-v2"},
            "artifacts": [{"parts": [
        {"metadata": {"adk_type": "function_call"},
         "data": {"id": "outer", "name": agent, "args": {"request": "chunk"}}},
        {"metadata": {"adk_type": "function_response"},
         "data": {"id": "outer", "name": agent,
                  "response": {"result": "model prose",
                               "subagent_session_id": "child-context"}}},
        {"text": json.dumps(business), "metadata": {"runtime_rendered": True}},
    ]}]}
    expected = {"trajectory": ["context"], "context": {
        "tool": "get_chunk_by_id", "arguments": {"chunk_id": "chunk-1"}}}
    result = inspect_workflow(
        {"result": {"task": root}}, expected, bundle,
        child_reader=lambda sid: {"result": {"task": child}})
    assert result["verdict"] == "PASS"
    assert "trusted runtime renderer" in result["children"][0]["reason"]
    assert result["reason"] == "workflow trajectory and outcomes verified"


def test_usage_deduplicates_artifacts_not_remote_aggregates():
    from jenkins.python.llm_agent_cd.workflow_evidence import own_usage
    artifact = {"artifactId": "llm-call-1", "metadata": {"adk_usage_metadata": {"promptTokenCount": 10, "candidatesTokenCount": 3}}}
    assert own_usage({"artifacts": [artifact, artifact], "metadata": {"kagent_usage_metadata": {"input_tokens": 1000}}}) == {"input_tokens": 10, "output_tokens": 3}
    assert own_usage({}) == {}


def test_metrics_have_unique_series_and_no_fabricated_usage(bundle):
    from apps.agentic.llm_ab_router.workflow_metrics import render
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): return self
        def fetchall(self): return []
        def fetchone(self): return {"at": None}
    class DB:
        def connect(self): return Connection()
    state = {"phase": "IDLE", "champion": bundle, "baseline": bundle,
             "pending": bundle,
             "fixtures": [{"id": "one"}, {"id": "two"}, {"id": "three"}]}
    wire = render(DB(), state)
    series = [l.rsplit(" ", 1)[0] for l in wire.splitlines() if not l.startswith("#")]
    assert len(series) == len(set(series))
    assert "input_tokens_total" not in wire
    assert "telemetry_timestamp" not in wire
    assert "recsys_workflow_info" in wire
    assert "recsys_workflow_evaluation_expected" in wire
    assert wire.count("recsys_workflow_evaluation_expected") == 2
    assert "} 3\n" in wire


def test_metrics_keep_closed_gate_windows_after_promotion(bundle):
    from apps.agentic.llm_ab_router.workflow_metrics import render

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): return self
        def fetchall(self): return []
        def fetchone(self): return {"at": None}

    class DB:
        def connect(self): return Connection()

    windows = [
        {"phase": "CANARY", "source": "live_test", "verdict": "PASS",
         "start": 1, "end": 601, "latency_ratio": 1.2,
         "observation": {"champion": {"count": 81, "p95": 11.9, "errors": 0,
                                          "contract_failures": 0, "unknown": 0},
                         "challenger": {"count": 5, "p95": 6.8, "errors": 0,
                                            "contract_failures": 0, "unknown": 0}}},
        {"phase": "AB", "source": "live_test", "verdict": "PASS",
         "start": 602, "end": 1202, "latency_ratio": 1.2,
         "observation": {"champion": {"count": 12, "p95": 11.934925, "errors": 0,
                                          "contract_failures": 0, "unknown": 0},
                         "challenger": {"count": 8, "p95": 3.93607615, "errors": 0,
                                            "contract_failures": 0, "unknown": 0}}},
    ]
    state = {"phase": "COMPLETED", "experiment_id": "exp-1", "champion": bundle,
             "baseline": bundle, "pending": candidate(bundle, True),
             "gate_windows": windows, "gate_evidence": windows[-1]}
    wire = render(DB(), state)
    assert 'recsys_workflow_gate_window_info{' in wire
    assert 'phase="CANARY"' in wire and 'phase="AB"' in wire
    assert 'window_index="0"' in wire and 'window_index="1"' in wire
    assert 'latest="true"' in wire
    assert 'recsys_workflow_gate_window_p95_seconds{' in wire
    assert ' 11.934925' in wire and ' 3.93607615' in wire
    series = [line.rsplit(" ", 1)[0] for line in wire.splitlines() if not line.startswith("#")]
    assert len(series) == len(set(series))


def test_loki_projection_is_redacted_and_repeatable(bundle):
    from apps.agentic.llm_ab_router.workflow_events import snapshot_events
    state = {"phase": "DEPLOY", "experiment_id": "example", "baseline": bundle, "pending": candidate(bundle, True),
             "events": [{"phase": "DEPLOY", "at": 1, "credentials": "do-not-export", "pending": bundle}]}
    events = snapshot_events(state)
    assert events == snapshot_events(state)
    assert "do-not-export" not in json.dumps(events)
    assert all(e["event_id"] for e in events)
