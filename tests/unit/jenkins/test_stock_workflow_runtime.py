# ruff: noqa: F401, F811
from copy import deepcopy
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from tests.unit.jenkins.test_llm_workflow import bundle
from tests.unit.jenkins.test_llm_agent_cd import champion
from apps.agentic.llm_ab_router.trigger import candidate_from_config
from jenkins.python.llm_agent_cd.manifests import resources
from jenkins.python.llm_agent_cd.release import release
from jenkins.python.llm_agent_cd.runtime_probe import stock_probe_verdict
from jenkins.python.llm_agent_cd.workflow import TOKENS
from apps.agentic.llm_ab_router.server import render_workflow_result
from jenkins.python.llm_agent_cd.workflow_contract import (
    COMPACT_PROMPTS,
    revise_stock_runtime_and_b8646_serving,
)


def with_marker(bundle):
    raw = {k: deepcopy(bundle[k]) for k in (
        "schema_version", "scope", "global_generation", "agent_overrides",
        "llm", "agents", "bindings")}
    for role in raw["agents"]:
        raw["agents"][role]["systemMessage"] += (
            "\nRuntime model configuration revision: stock-test.\n")
    return release(raw)


def stock_baseline(bundle):
    old = with_marker(bundle)
    llm = json.loads(Path(
        "configs/llm-ab/catalog/qwen3.5-0.8b-q4_0-b8646-stock-adk-v1.json"
    ).read_text())
    raw = {k: deepcopy(old[k]) for k in (
        "schema_version", "scope", "global_generation", "agent_overrides",
        "llm", "agents", "bindings")}
    raw["llm"] = deepcopy(llm)
    # Model identity changes to the reviewed b8646 serving build before the
    # migration function proves the artifact and quantization stayed fixed.
    host = "rec-llm-" + release({
        "config": raw["global_generation"], "llm": llm,
        "agent": raw["agents"]["coordinator"],
        "binding": raw["bindings"]["coordinator"],
    })["llm_version_id"][:20] + ".kagent.svc.cluster.local"
    for binding in raw["bindings"].values():
        binding.update(managed_backend=True, backend_url="http://" + host + ":8000/v1")
    source = release(raw)
    return revise_stock_runtime_and_b8646_serving(
        source, llm, "registry/golang-adk@sha256:" + "a" * 64,
        "registry/router@sha256:" + "d" * 64)


def test_workflow_rejects_direct_mcp_on_coordinator(bundle):
    raw = {k: deepcopy(bundle[k]) for k in (
        "schema_version", "scope", "global_generation", "agent_overrides",
        "llm", "agents", "bindings")}
    raw["agents"]["coordinator"]["tools"].append({
        "type": "McpServer", "mcpServer": {"name": "forbidden", "toolNames": ["x"]}})
    with pytest.raises(ValueError, match="only the two A2A"):
        release(raw)


def test_stock_migration_removes_guard_surface_and_pins_promotable_a2a_baseline(bundle):
    revised, audit = stock_baseline(bundle)
    assert "execution_policy" not in revised
    assert set(t["agent"]["name"] for t in revised["agents"]["coordinator"]["tools"]) == set(TOKENS.values())
    assert all(t["type"] == "Agent" for t in revised["agents"]["coordinator"]["tools"])
    assert "null MUST remain null" in revised["agents"]["context"]["systemMessage"]
    assert "NEVER emit [] for null input" in revised["agents"]["context"]["systemMessage"]
    assert "all three fields user_id, candidate_item_ids and top_k are required" in revised["agents"]["context"]["systemMessage"]
    assert 'output exactly {"done":true}' in revised["agents"]["context"]["systemMessage"]
    assert 'output exactly {"done":true}' in revised["agents"]["recommendation"]["systemMessage"]
    coordinator_prompt = revised["agents"]["coordinator"]["systemMessage"]
    assert "contains _context_" in coordinator_prompt
    assert "SINGLE_CONTEXT" in coordinator_prompt
    assert "adapter replaces the terminal marker with structured JSON" in coordinator_prompt
    assert "MODE=SINGLE_RECOMMENDATION" in coordinator_prompt
    assert "text after RECOMMENDATION_REQUEST=" in coordinator_prompt
    assert "text after CONTEXT_REQUEST=" in coordinator_prompt
    assert "MODE=MISSING_USER" in coordinator_prompt
    assert "Preserve every character and all text after `=`" in coordinator_prompt
    assert "Never put this system message" in coordinator_prompt
    assert "output exactly {\"done\":true}" in coordinator_prompt
    assert "Never call the same route twice" in coordinator_prompt
    assert "Never call ask_user, submit_result" in coordinator_prompt
    assert [tool["agent"]["name"] for tool in revised["agents"]["coordinator"]["tools"]] == [
        TOKENS["context"], TOKENS["recommendation"]]
    assert audit["coordinator_tool_order"] == ["context", "recommendation"]
    assert revised["agents"]["context"]["systemMessage"].startswith(COMPACT_PROMPTS["context"])
    assert audit["promotable"] is True
    assert revised["runtime"] == {
        "go_adk_image": "registry/golang-adk@sha256:" + "a" * 64,
        "a2a_name_profile": "role-v1",
        "a2a_description_profile": "role-terminal-v2",
        "deterministic_output": "trusted-child-tool-results-v2",
        "specialist_terminal_output": "done-marker-v1",
    }
    assert audit["runtime_identity_in_release_hash"] is True
    objects = resources(revised, "kagent", "adapter@sha256:" + "b" * 64, "secret")
    agents = [o for o in objects if o["kind"] == "SandboxAgent"]
    by_role = {o["metadata"]["labels"]["recsys.ai/agent-role"]: o for o in agents}
    assert {role: obj["spec"]["description"] for role, obj in by_role.items()} == {
        "coordinator": "A2A workflow coordinator with exactly Context and Recommendation specialist tools.",
        "context": "Context specialist. Call once with the original Context request. Its returned response is terminal completed data; never send it to this or another specialist.",
        "recommendation": "Recommendation specialist. Call once with the original Recommendation request. Its returned response is terminal completed data; never send it to this or another specialist.",
    }
    assert by_role["context"]["metadata"]["name"].startswith("rec-ab-context-")
    assert by_role["recommendation"]["metadata"]["name"].startswith(
        "rec-ab-recommendation-")
    coordinator_tools = by_role["coordinator"]["spec"]["declarative"]["tools"]
    assert {tool["agent"]["name"] for tool in coordinator_tools} == {
        by_role["context"]["metadata"]["name"],
        by_role["recommendation"]["metadata"]["name"],
    }
    coordinator_prompt = by_role["coordinator"]["spec"]["declarative"]["systemMessage"]
    assert "kagent__NS__rec_ab_context_" in coordinator_prompt
    assert "kagent__NS__rec_ab_recommendation_" in coordinator_prompt
    assert all("recsys.ai/workflow-execution" not in o["metadata"]["annotations"] for o in agents)
    assert all("kagent-postgresql.kagent.svc.cluster.local" not in
               o["spec"]["sandbox"]["network"]["allowedDomains"] for o in agents)
    deployments = [o for o in objects if o["kind"] == "Deployment"]
    assert len(deployments) == 2
    env = {entry["name"]: entry["value"] for entry in
           deployments[0]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["WORKFLOW_OUTPUT_PROFILE"] == "trusted-child-tool-results-v2"
    assert env["AB_KAGENT_GRPC_TARGET"] == "kagent-controller.kagent.svc.cluster.local:8084"
    assert "_context_" in env["WORKFLOW_CONTEXT_TOOL"]
    assert "_recommendation_" in env["WORKFLOW_RECOMMENDATION_TOOL"]


def test_adapter_renders_only_exact_native_a2a_trajectories():
    rec = "kagent__NS__rec_ab_recommendation_a"
    ctx = "kagent__NS__rec_ab_context_b"
    task = {"status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{"parts": [
        {"metadata": {"adk_type": "function_call"},
         "data": {"id": "r", "name": rec, "args": {"request": "one"}}},
        {"metadata": {"adk_type": "function_response"},
         "data": {"id": "r", "name": rec,
                  "response": {"result": '{"items":[],"user_id":218}',
                               "subagent_session_id": "child-r"}}},
        {"metadata": {"adk_type": "function_call"},
         "data": {"id": "c", "name": ctx, "args": {"request": "two"}}},
        {"metadata": {"adk_type": "function_response"},
         "data": {"id": "c", "name": ctx,
                  "response": {"result": '{"chunk_id":"chunk-1"}',
                               "subagent_session_id": "child-c"}}},
        {"text": "model output is incomplete"},
    ]}]}
    body = {"result": {"task": task}}
    rendered, changed = render_workflow_result(body, ctx, rec)
    assert changed is True and rendered is not body
    assert json.loads(rendered["result"]["task"]["artifacts"][0]["parts"][-1]["text"]) == {
        "recommendation": {"items": [], "user_id": 218},
        "context": {"chunk_id": "chunk-1"},
    }
    assert rendered["result"]["task"]["metadata"] == {
        "runtime_rendered": True, "runtime_output_profile": "a2a-results-v1"}
    assert body["result"]["task"]["artifacts"][0]["parts"][-1]["text"] == (
        "model output is incomplete")
    wrong = deepcopy(body)
    wrong["result"]["task"]["artifacts"][0]["parts"][0]["data"]["name"] = ctx
    unchanged, changed = render_workflow_result(wrong, ctx, rec)
    assert changed is False and unchanged is wrong

    wrapped = deepcopy(body)
    for part in wrapped["result"]["task"]["artifacts"][0]["parts"]:
        if part.get("metadata", {}).get("adk_type") == "function_response":
            value = json.loads(part["data"]["response"]["result"])
            part["data"]["response"]["result"] = json.dumps({"output": value})
    rendered, changed = render_workflow_result(wrapped, ctx, rec)
    assert changed is True
    assert "output" not in json.loads(
        rendered["result"]["task"]["artifacts"][0]["parts"][-1]["text"])

    prose = deepcopy(body)
    for part in prose["result"]["task"]["artifacts"][0]["parts"]:
        if part.get("metadata", {}).get("adk_type") == "function_response":
            part["data"]["response"]["result"] = "model summarized the result"
    children = {
        "child-r": {"result": {"task": {"status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"parts": [
                {"metadata": {"adk_type": "function_call"},
                 "data": {"id": "ir", "name": "get_personalized_recommendations", "args": {}}},
                {"metadata": {"adk_type": "function_response"},
                 "data": {"id": "ir", "name": "get_personalized_recommendations",
                          "response": {"output": {"items": [], "user_id": 218}}}},
            ]}]}}},
        "child-c": {"result": {"task": {"status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"parts": [
                {"metadata": {"adk_type": "function_call"},
                 "data": {"id": "ic", "name": "get_chunk_by_id", "args": {}}},
                {"metadata": {"adk_type": "function_response"},
                 "data": {"id": "ic", "name": "get_chunk_by_id",
                          "response": {"output": {"chunk_id": "chunk-1"}}}},
            ]}]}}},
    }
    rendered, changed = render_workflow_result(
        prose, ctx, rec, child_reader=children.__getitem__,
        output_profile="trusted-child-tool-results-v2")
    assert changed is True
    assert json.loads(rendered["result"]["task"]["artifacts"][0]["parts"][-1]["text"]) == {
        "recommendation": {"items": [], "user_id": 218},
        "context": {"chunk_id": "chunk-1"},
    }
    assert rendered["result"]["task"]["metadata"]["runtime_output_profile"] == (
        "trusted-child-tool-results-v2")


def test_stock_migration_filters_legacy_direct_mcp_before_strict_validation(bundle):
    old, _ = stock_baseline(bundle)
    old["agents"]["coordinator"]["tools"].append({
        "type": "McpServer",
        "mcpServer": {"name": "legacy-direct-mcp", "toolNames": ["forbidden"]},
    })
    llm = json.loads(Path(
        "configs/llm-ab/catalog/qwen3.5-0.8b-q4_0-b8646-stock-adk-v1.json"
    ).read_text())
    revised, _ = revise_stock_runtime_and_b8646_serving(
        old, llm, "registry/golang-adk@sha256:" + "a" * 64,
        "registry/router@sha256:" + "d" * 64)
    assert all(tool["type"] == "Agent" for tool in revised["agents"]["coordinator"]["tools"])


def test_candidate_has_no_runtime_policy_overlay(bundle):
    baseline, _ = stock_baseline(bundle)
    config = {"schema_version": 1, "scope": "workflow",
        "baseline_workflow_release_id": baseline["release_id"],
        "global_generation": {**baseline["global_generation"], "temperature": "0.2"},
        "llm_release_ref": baseline["llm_version_id"],
        "experiment_type": "config_only", "policy_ref": "workflow-production"}
    candidate = candidate_from_config(baseline, config, baseline["llm"])
    assert "execution_policy" not in candidate
    assert candidate["runtime"] == baseline["runtime"]


def test_runtime_identity_changes_release_id_and_cannot_change_in_ab(bundle):
    baseline, _ = stock_baseline(bundle)
    raw = {k: deepcopy(baseline[k]) for k in (
        "schema_version", "scope", "global_generation", "agent_overrides",
        "llm", "agents", "bindings", "runtime")}
    raw["runtime"]["go_adk_image"] = "registry/golang-adk@sha256:" + "c" * 64
    changed = release(raw)
    assert changed["release_id"] != baseline["release_id"]
    from jenkins.python.llm_agent_cd.workflow import validate_workflow
    with pytest.raises(ValueError, match="runtime must remain fixed"):
        validate_workflow(baseline, changed, "config_only")


def test_stock_probe_requires_exact_null_and_exactly_one_call():
    call = {"id": "c1", "name": "get_user_online_features",
            "args": {"user_id": 218, "candidate_item_ids": None, "top_k": 2}}
    response = {"id": "c1", "name": call["name"], "response": {"items": []}}
    task = {"status": {"state": "completed"}, "artifacts": [{"parts": [
        {"metadata": {"adk_type": "function_call"}, "data": call},
        {"metadata": {"adk_type": "function_response"}, "data": response},
    ]}]}
    body = {"result": {"task": task}}
    assert stock_probe_verdict(body, call["name"], call["args"])
    assert not stock_probe_verdict(body, call["name"], call["args"], require_final=True)
    task["artifacts"].append({"parts": [{"text": '{"items":[]}' }]})
    assert stock_probe_verdict(body, call["name"], call["args"], require_final=True)
    response["response"] = {"output": {"items": []}}
    assert stock_probe_verdict(body, call["name"], call["args"], require_final=True)
    task["artifacts"][-1]["parts"][-1]["text"] = '{"done":true}'
    assert stock_probe_verdict(body, call["name"], call["args"], terminal_marker=True)
    assert not stock_probe_verdict(body, call["name"], call["args"], require_final=True)
    task["artifacts"][0]["parts"][0]["data"]["args"]["candidate_item_ids"] = []
    assert not stock_probe_verdict(body, call["name"], call["args"] | {"candidate_item_ids": None})


def test_coordinator_compatibility_gate_requires_exact_route_terminal_and_no_ask_user(
        bundle, monkeypatch):
    from jenkins.python.llm_agent_cd.runtime_probe import _result_from_body
    from jenkins.python.llm_agent_cd.workflow_contract import (
        revise_coordinator_native_sequential_baseline,
        revise_coordinator_terminal_prompt,
    )

    stock, _ = stock_baseline(bundle)
    v30, _ = revise_coordinator_terminal_prompt(stock)
    workflow, _ = revise_coordinator_native_sequential_baseline(v30)
    agent = "kagent__NS__rec_ab_recommendation_test"
    route_args = {"request": "recommend user 218"}
    child = {"result": {"task": {"status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"parts": [
            {"metadata": {"adk_type": "function_call"}, "data": {
                "id": "inner", "name": "get_personalized_recommendations",
                "args": {"user_id": 218, "candidate_item_ids": None, "top_k": 3}}},
            {"metadata": {"adk_type": "function_response"}, "data": {
                "id": "inner", "name": "get_personalized_recommendations",
                "response": {"output": {"items": [], "user_id": 218}}}},
            {"text": '{"done":true}'},
        ]}]}}}

    class Reader:
        def __init__(self, principal, agents):
            assert principal == "workflow-infrastructure-preflight"
            assert agents == [agent]

        def __call__(self, session_id):
            assert session_id == "child-1"
            return child

        def close(self):
            pass

    import apps.agentic.llm_ab_router.child_tasks as child_tasks
    monkeypatch.setattr(child_tasks, "ChildTasks", Reader)
    root = {"status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{"parts": [
        {"metadata": {"adk_type": "function_call"}, "data": {
            "id": "outer", "name": agent, "args": route_args}},
        {"metadata": {"adk_type": "function_response"}, "data": {
            "id": "outer", "name": agent,
            "response": {"subagent_session_id": "child-1"}}},
        {"text": '{"items":[],"user_id":218}'},
    ]}]}
    spec = [{"role": "recommendation", "agent": agent,
             "tool": "get_personalized_recommendations",
             "args": {"user_id": 218, "candidate_item_ids": None, "top_k": 3}}]
    result = _result_from_body(
        {"result": {"task": root}}, workflow, "probe", "coordinator",
        agent, route_args, "request", spec)
    assert result["verdict"] == "PASS"
    assert result["compatibility_gate"] == {
        "expected_a2a_calls": [agent],
        "observed_function_calls": [agent],
        "expected_route_arguments": [route_args],
        "observed_route_arguments": [route_args],
        "ask_user_calls": 0,
        "extra_function_calls": 0,
        "terminal_state": "TASK_STATE_COMPLETED",
        "terminal_output_verified": True,
        "trajectory_verified": True,
    }

    bad = deepcopy(root)
    bad["artifacts"][0]["parts"].insert(2, {
        "metadata": {"adk_type": "function_call"},
        "data": {"id": "ask", "name": "ask_user", "args": {"question": "retry?"}},
    })
    failed = _result_from_body(
        {"result": {"task": bad}}, workflow, "probe", "coordinator",
        agent, route_args, "request", spec)
    assert failed["verdict"] == "FAIL"
    assert failed["compatibility_gate"]["ask_user_calls"] == 1
    assert failed["compatibility_gate"]["extra_function_calls"] == 1


def test_repository_contains_no_custom_adk_guard_patch():
    assert not list(Path("ops/gcp/patches").glob("*kagent*.patch"))
    assert not Path("jenkins/python/llm_agent_cd/execution_policy.py").exists()
    assert not Path("ops/helm/kagent_workflow_postrender.py").exists()
    assert not Path("ops/helm/kagent_stock_adk_postrender.py").exists()
    assert not Path("ops/gcp/build_kagent_source.sh").exists()
    assert not Path("ops/gcp/cloudbuild_kagent_source.yaml").exists()
    assert not Path("infra/helm/substrate-mtls-bootstrap").exists()
    assert not Path("jenkins/scripts/deploy/agentic/sandbox.sh").exists()
    runtime_sources = "\n".join(
        path.read_text()
        for root in (
            Path("jenkins/python/llm_agent_cd"),
            Path("apps/agentic/llm_ab_router"),
        )
        for path in root.rglob("*.py")
    )
    assert "task_store_user_context" not in runtime_sources
    assert "authenticated-call-context-v1" not in runtime_sources
    assert '"custom_guard"' not in runtime_sources


def test_runtime_probe_requires_child_task_owner_evidence():
    source = Path("jenkins/python/llm_agent_cd/runtime_probe.py").read_text()
    assert '"workflow-infrastructure-preflight", [spec["agent"]]' in source
    assert "child_task_owner_verified" in source
    assert "stock-context-exact-chunk-v29" in source
    assert "stock-coordinator-recommendation-a2a-owner-v29" in source
    assert "stock-coordinator-context-a2a-owner-v29" in source
    assert "stock-coordinator-composite-a2a-owner-v29" in source
    assert '"candidate-coordinator"' in source
    assert '"prompt-baseline-coordinator"' in source
    assert 'prefix + "-recommendation-a2a-v33"' in source
    assert 'prefix + "-context-a2a-v33"' in source
    assert 'prefix + "-composite-a2a-v33"' in source
    assert "wait_adapter_ready(endpoint)" in source
    assert 'observed_route_args == expected_route_args' in source
    assert 'ask_user_calls == 0' in source
    assert '"terminal_output_verified"' in source
    assert "REQUEST_TIMEOUT_SECONDS = 600" in source
    assert "recovered_from_existing_task" in source
    assert "intent identity mismatch; do not replay" in source
    baseline = Path("jenkins/python/llm_agent_cd/baseline_prepare.py").read_text()
    assert "AB_KAGENT_GRPC_TARGET" in baseline


def test_workflow_cd_requires_native_candidate_a2a_pass_evidence():
    from jenkins.python.llm_agent_cd.runtime_probe import (
        candidate_coordinator_probe_ids,
        verified_candidate_a2a_evidence,
    )
    from jenkins.python.llm_agent_cd.release import digest

    release_id = "a" * 64
    reports = {}
    for probe_id in candidate_coordinator_probe_ids():
        calls = ["recommendation", "context"] if "composite" in probe_id else [
            "recommendation" if "recommendation" in probe_id else "context"
        ]
        arguments = [{"request": value} for value in calls]
        reports[probe_id] = {
            "source": "infrastructure_test",
            "role": "coordinator",
            "release_id": release_id,
            "probe": probe_id,
            "verdict": "PASS",
            "offline_requests": 0,
            "synthetic_requests": 0,
            "child_task_owner_verified": True,
            "child_task_evidence_checksum": "b" * 64,
            "compatibility_gate": {
                "trajectory_verified": True,
                "terminal_output_verified": True,
                "terminal_state": "TASK_STATE_COMPLETED",
                "ask_user_calls": 0,
                "extra_function_calls": 0,
                "expected_a2a_calls": calls,
                "observed_function_calls": calls,
                "expected_route_arguments": arguments,
                "observed_route_arguments": arguments,
            },
        }

    def get_object(**kwargs):
        probe_id = kwargs["Key"].split("/")[-2]
        return {"Body": io.BytesIO(json.dumps(reports[probe_id]).encode())}

    store = SimpleNamespace(
        bucket="evidence", client=SimpleNamespace(get_object=get_object)
    )
    evidence = verified_candidate_a2a_evidence(
        store, {"release_id": release_id}
    )
    assert evidence == {
        "verdict": "PASS",
        "release_id": release_id,
        "probe_ids": candidate_coordinator_probe_ids(),
        "evidence_checksum": digest(
            [reports[probe] for probe in candidate_coordinator_probe_ids()]
        ),
    }

    reports[candidate_coordinator_probe_ids()[0]]["verdict"] = "FAIL"
    with pytest.raises(ValueError, match="did not pass"):
        verified_candidate_a2a_evidence(store, {"release_id": release_id})

    def missing(**_kwargs):
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    store.client.get_object = missing
    with pytest.raises(ValueError, match="HOLD"):
        verified_candidate_a2a_evidence(store, {"release_id": release_id})
