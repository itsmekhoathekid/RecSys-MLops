"""Inspect pinned kagent A2A task history in memory; persist assertions, not prompts."""

from __future__ import annotations

import json
import re

TOOL = "get_personalized_recommendations"
COMPLETED = {"completed", "TASK_STATE_COMPLETED"}
INPUT_REQUIRED = {"input-required", "TASK_STATE_INPUT_REQUIRED"}


def native_ask_user_confirmation(call, ask_calls):
    """Recognize the stock Go ADK HITL wrapper for one native ask_user call.

    The framework mirrors ``ask_user`` and then emits an internal
    ``adk_request_confirmation`` event carrying the original function call.
    It is lifecycle evidence, not a second dependency execution.  Match its
    ID, name and arguments exactly so an unrelated/forged extra call still
    fails the contract.
    """
    if call.get("name") != "adk_request_confirmation" or len(ask_calls) != 1:
        return False
    args = call.get("args", {})
    original = args.get("originalFunctionCall", {})
    confirmation = args.get("toolConfirmation", {})
    ask = ask_calls[0]
    return (
        original.get("id") == ask.get("id")
        and original.get("name") == "ask_user"
        and original.get("args") == ask.get("args")
        and confirmation.get("confirmed") is False
    )


def task_of(body):
    result = body.get("result", {})
    return result.get("task", result)


def recommendation(value):
    if isinstance(value, dict):
        if {"items", "user_id", "model_version"} <= value.keys():
            return value
        for nested in value.values():
            found = recommendation(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = recommendation(nested)
            if found is not None:
                return found
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        if isinstance(parsed, (dict, list)):
            return recommendation(parsed)
    return None


def inspect(body, expected=None, message_id=None):
    if body.get("error"):
        return {
            "verdict": "FAIL",
            "reason": "A2A error",
            "error": True,
            "contract_failure": False,
        }
    task = task_of(body)
    state = task.get("status", {}).get("state")
    native_missing_user = bool(expected and expected.get("missing_user"))
    if state not in COMPLETED and not (
        native_missing_user and state in INPUT_REQUIRED
    ):
        return {
            "verdict": "HOLD",
            "reason": "task not completed",
            "error": False,
            "contract_failure": False,
        }
    history = task.get("history", [])
    if message_id:
        indices = [i for i, m in enumerate(history) if m.get("messageId") == message_id]
        if indices:
            history = history[indices[-1] :]
        elif sum(h.get("role") in {"user", "ROLE_USER"} for h in history) > 1:
            return {
                "verdict": "HOLD",
                "reason": "cannot isolate this turn from session history",
                "error": False,
                "contract_failure": False,
            }
    parts = [p for h in history for p in h.get("parts", [])]
    # Pinned kagent emits tool events in Task.artifacts, while history may only
    # contain the user message. Some transports duplicate events in both places.
    history_events = {
        (p.get("metadata", {}).get("adk_type"), p.get("data", {}).get("id")): p.get(
            "data"
        )
        for p in parts
        if p.get("data", {}).get("id")
    }
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            data = part.get("data", {})
            key = (part.get("metadata", {}).get("adk_type"), data.get("id"))
            if not data.get("id") or history_events.get(key) != data:
                parts.append(part)
    calls = [
        p.get("data", {})
        for p in parts
        if p.get("metadata", {}).get("adk_type") == "function_call"
    ]
    responses = [
        p.get("data", {})
        for p in parts
        if p.get("metadata", {}).get("adk_type") == "function_response"
    ]
    answer_parts = [p for a in task.get("artifacts", []) for p in a.get("parts", [])]
    if not answer_parts:
        answer_parts = task.get("status", {}).get("message", {}).get("parts", [])
    if not answer_parts:
        answer_parts = next(
            (
                h.get("parts", [])
                for h in reversed(history)
                if h.get("role") in {"agent", "ROLE_AGENT"}
                and any("text" in p for p in h.get("parts", []))
            ),
            [],
        )
    text = " ".join(p.get("text", "") for p in answer_parts).strip()

    def result(verdict, reason):
        evidence = {
            "verdict": verdict,
            "reason": reason,
            "error": False,
            "contract_failure": verdict == "FAIL",
        }
        # Optional per-invocation usage only; never borrow a shared backend counter.
        usage = task.get("metadata", {}).get("usage", {})
        if not usage:
            events = {
                a["artifactId"]: a.get("metadata", {}).get("adk_usage_metadata")
                for a in task.get("artifacts", [])
                if a.get("artifactId")
                and a.get("metadata", {}).get("adk_usage_metadata")
            }
            for key, wire in (
                ("input_tokens", "promptTokenCount"),
                ("output_tokens", "candidatesTokenCount"),
            ):
                if events and all(
                    isinstance(v.get(wire), int) and v[wire] >= 0
                    for v in events.values()
                ):
                    usage[key] = sum(v[wire] for v in events.values())
        for key in ("input_tokens", "output_tokens"):
            if isinstance(usage.get(key), int) and usage[key] >= 0:
                evidence[key] = usage[key]
        return evidence

    if expected and expected.get("missing_user"):
        recommendation_calls = [call for call in calls if call.get("name") == TOOL]
        ask_calls = [call for call in calls if call.get("name") == "ask_user"]
        foreign_calls = [
            call
            for call in calls
            if call.get("name") not in {TOOL, "ask_user"}
            and not native_ask_user_confirmation(call, ask_calls)
        ]
        if recommendation_calls or foreign_calls:
            return result("FAIL", "dependency tool called without user ID")
        if state in INPUT_REQUIRED and len(ask_calls) != 1:
            return result("FAIL", "native clarification must call ask_user exactly once")
        if state in COMPLETED and calls:
            return result("FAIL", "completed clarification must not call a function")
        question_text = text
        if ask_calls:
            questions = ask_calls[0].get("args", {}).get("questions", [])
            if len(questions) != 1 or not isinstance(questions[0], dict):
                return result("FAIL", "native clarification must ask one question")
            question_text += " " + str(questions[0].get("question", ""))
        if not re.search(
            r"user.?id|user identifier|mã người dùng", question_text, re.I
        ):
            return result("FAIL", "missing-user answer does not ask for ID")
        if state in INPUT_REQUIRED:
            return result("PASS", "native ask_user clarification without dependency execution")
        return result("PASS", "terminal clarification without dependency execution")
    if not history or not text:
        return result("HOLD", "missing task history/final answer")
    if not calls or not responses:
        return result("HOLD", "missing function-call/response evidence")
    if (
        len(calls) != 1
        or len(responses) != 1
        or calls[0].get("name") != TOOL
        or responses[0].get("name") != TOOL
    ):
        return result("FAIL", "tool must be called exactly once")
    args = calls[0].get("args", {})
    if expected and args != expected["arguments"]:
        return result("FAIL", "tool arguments changed")
    source = recommendation(responses[0].get("response"))
    if source is None:
        return result("HOLD", "missing structured tool response")
    rendered = recommendation(
        text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    )
    if rendered is None:
        # Do not label free prose a pass merely because it contains an item ID.
        return result("HOLD", "final answer not machine-verifiable JSON")
    keys = ("user_id", "items", "model_version", "ab_variant", "ab_experiment_id")
    if any(rendered.get(k) != source.get(k) for k in keys):
        return result("FAIL", "ranking/score/metadata differs from tool output")
    if len(source["items"]) > args.get("top_k", 0) or source["user_id"] != args.get(
        "user_id"
    ):
        return result("FAIL", "tool result violates request bounds")
    if expected and expected.get("empty") and source["items"]:
        return result("FAIL", "empty-result fixture returned items")
    return result("PASS", "arguments, ranking and metadata preserved")
