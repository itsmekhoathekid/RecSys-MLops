from copy import deepcopy
import json

from apps.agentic.llm_ab_router.server import render_recommendation_result
from jenkins.python.llm_agent_cd.recommendation_evaluation import VERSION, evaluate
from tests.unit.jenkins.test_llm_agent_cd import task_fixture


def request(body, expected):
    return {
        "kind": "recommendation",
        "body": body,
        "expected": expected,
        "message_id": None,
        "release_id": "release-a",
        "assigned_release_id": "release-a",
        "duration_seconds": 1.25,
    }


def test_recommendation_scores_exact_tool_arguments_and_ranking():
    body, expected = task_fixture()
    result = evaluate(request(body, expected))
    scores = {row["name"]: row for row in result["scores"]}
    assert result["evaluator_version"] == VERSION
    assert result["verdict"] == "PASS"
    assert scores["trajectory_match"]["value"] is True
    assert scores["tool_arguments_match"]["value"] is True
    assert scores["duplicate_tool_calls"]["value"] == 0
    assert scores["ranking_preserved"]["value"] is True
    assert scores["citation_validity"]["status"] == "NOT_APPLICABLE"


def test_recommendation_duplicate_or_release_mismatch_fails():
    body, expected = task_fixture()
    task = body["result"]["task"]
    task["history"][0]["parts"].append(deepcopy(task["history"][0]["parts"][0]))
    bad = request(body, expected)
    bad["assigned_release_id"] = "release-b"
    result = evaluate(bad)
    scores = {row["name"]: row for row in result["scores"]}
    assert result["verdict"] == "FAIL"
    assert scores["release_consistency"]["status"] == "FAIL"
    assert scores["duplicate_tool_calls"]["status"] == "FAIL"


def test_missing_user_is_safe_without_any_dependency_call():
    body = {"result": {"task": {
        "status": {"state": "TASK_STATE_COMPLETED"},
        "history": [{"role": "ROLE_USER", "parts": [{"text": "recommend"}]}],
        "artifacts": [{"parts": [{"text": "Please provide user_id."}]}],
    }}}
    result = evaluate(request(body, {"missing_user": True}))
    scores = {row["name"]: row for row in result["scores"]}
    assert result["verdict"] == "PASS"
    assert scores["missing_user_safe"]["value"] is True
    assert scores["tool_arguments_match"]["status"] == "NOT_APPLICABLE"


def test_native_ask_user_input_required_is_safe_without_recommendation_call():
    ask = {
        "id": "ask-1",
        "name": "ask_user",
        "args": {"questions": [{"question": "Please provide your user ID."}]},
    }
    body = {"result": {"task": {
        "status": {
            "state": "TASK_STATE_INPUT_REQUIRED",
            "message": {"parts": []},
        },
        "history": [{
            "role": "ROLE_USER",
            "parts": [{
                "metadata": {"adk_type": "function_call"},
                "data": ask,
            }, {
                # Stock Go ADK HITL lifecycle wrapper. This is not a second
                # dependency execution and must match the ask_user call.
                "metadata": {
                    "adk_type": "function_call",
                    "adk_is_long_running": True,
                },
                "data": {
                    "id": "adk-confirm-1",
                    "name": "adk_request_confirmation",
                    "args": {
                        "originalFunctionCall": deepcopy(ask),
                        "toolConfirmation": {
                            "confirmed": False,
                            "hint": "Please provide your user ID.",
                            "payload": None,
                        },
                    },
                },
            }],
        }],
        "artifacts": [],
    }}}
    result = evaluate(request(body, {"missing_user": True}))
    scores = {row["name"]: row for row in result["scores"]}
    assert result["verdict"] == "PASS"
    assert scores["trajectory_match"]["value"] is True
    assert scores["missing_user_safe"]["value"] is True
    assert scores["duplicate_tool_calls"]["value"] == 0


def test_mismatched_adk_confirmation_is_an_extra_foreign_call():
    body = {"result": {"task": {
        "status": {
            "state": "TASK_STATE_INPUT_REQUIRED",
            "message": {"parts": [{"text": "Please provide user_id."}]},
        },
        "history": [{"role": "ROLE_USER", "parts": [
            {"metadata": {"adk_type": "function_call"}, "data": {
                "id": "ask-1", "name": "ask_user", "args": {
                    "questions": [{"question": "Please provide your user ID."}]
                },
            }},
            {"metadata": {"adk_type": "function_call"}, "data": {
                "id": "adk-confirm-1", "name": "adk_request_confirmation",
                "args": {
                    "originalFunctionCall": {
                        "id": "different", "name": "ask_user", "args": {},
                    },
                    "toolConfirmation": {"confirmed": False},
                },
            }},
        ]}],
        "artifacts": [],
    }}}
    result = evaluate(request(body, {"missing_user": True}))
    scores = {row["name"]: row for row in result["scores"]}
    assert result["verdict"] == "FAIL"
    assert scores["trajectory_match"]["status"] == "FAIL"
    assert scores["missing_user_safe"]["status"] == "FAIL"


def test_native_ask_user_repetition_or_dependency_call_fails():
    body = {"result": {"task": {
        "status": {
            "state": "TASK_STATE_INPUT_REQUIRED",
            "message": {"parts": [{"text": "Please provide user_id."}]},
        },
        "history": [{"role": "ROLE_USER", "parts": [
            {"metadata": {"adk_type": "function_call"},
             "data": {"id": "ask-1", "name": "ask_user", "args": {}}},
            {"metadata": {"adk_type": "function_call"},
             "data": {"id": "ask-2", "name": "ask_user", "args": {}}},
            {"metadata": {"adk_type": "function_call"},
             "data": {"id": "tool-1", "name": "get_personalized_recommendations",
                      "args": {"user_id": 1001, "candidate_item_ids": None, "top_k": 3}}},
        ]}],
        "artifacts": [],
    }}}
    result = evaluate(request(body, {"missing_user": True}))
    scores = {row["name"]: row for row in result["scores"]}
    assert result["verdict"] == "FAIL"
    assert scores["trajectory_match"]["status"] == "FAIL"
    assert scores["missing_user_safe"]["status"] == "FAIL"


def test_adapter_renders_one_trusted_tool_result_without_another_call():
    body, _ = task_fixture()
    task = body["result"]["task"]
    call, response = task["history"][0]["parts"]
    call["data"]["id"] = "call-1"
    response["data"]["id"] = "call-1"
    expected = response["data"]["response"]
    task["artifacts"][0]["parts"][0]["text"] = "model prose"
    rendered, changed = render_recommendation_result(body)
    assert changed is True
    assert json.loads(rendered["result"]["task"]["artifacts"][0]["parts"][0]["text"]) == expected
    assert rendered["result"]["task"]["metadata"] == {
        "runtime_rendered": True,
        "runtime_output_profile": "trusted-tool-result-v1",
    }
    duplicate = deepcopy(body)
    second_call = deepcopy(call)
    second_call["data"]["id"] = "call-2"
    duplicate["result"]["task"]["history"][0]["parts"].append(second_call)
    unchanged, changed = render_recommendation_result(duplicate)
    assert changed is False and unchanged is duplicate
