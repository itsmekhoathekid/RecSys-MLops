"""Deterministic Recommendation-only scores; never calls an LLM or tool."""

from __future__ import annotations

import json
import math

from .evidence import (
    COMPLETED,
    INPUT_REQUIRED,
    TOOL,
    inspect,
    native_ask_user_confirmation,
    recommendation,
    task_of,
)
from .release import digest

VERSION = "recommendation-code-v2"
HARD = (
    "trajectory_match",
    "release_consistency",
    "tool_arguments_match",
    "duplicate_tool_calls",
    "missing_user_safe",
    "schema_valid",
    "ranking_preserved",
    "empty_result_correct",
    "functional_success",
)


def score(name, value=None, *, status=None, reason=""):
    if status is None:
        status = (
            "PASS"
            if value is True or (name == "duplicate_tool_calls" and value == 0)
            else "FAIL"
        )
    return {
        "name": name,
        "value": value,
        "status": status,
        "reason": reason,
        "required": name in HARD,
    }


def _events(task, kind):
    """Read each native ADK event once across history and artifacts."""
    found = []
    seen = set()
    for index, part in enumerate([
        *(p for message in task.get("history", []) for p in message.get("parts", [])),
        *(p for artifact in task.get("artifacts", []) for p in artifact.get("parts", [])),
    ]):
        if part.get("metadata", {}).get("adk_type") != kind:
            continue
        data = part.get("data", {})
        # Only a native call ID proves that history/artifacts are duplicate
        # representations of one event. Missing IDs stay distinct and fail the
        # contract rather than being silently collapsed.
        identity = (kind, data.get("id")) if data.get("id") else (kind, index)
        if identity not in seen:
            seen.add(identity)
            found.append(data)
    return found


def _answer(task):
    parts = [
        p
        for artifact in task.get("artifacts", [])
        for p in artifact.get("parts", [])
        if "text" in p
    ]
    if not parts:
        parts = task.get("status", {}).get("message", {}).get("parts", [])
    if not parts:
        parts = next(
            (
                message.get("parts", [])
                for message in reversed(task.get("history", []))
                if message.get("role") in {"agent", "ROLE_AGENT"}
                and any("text" in part for part in message.get("parts", []))
            ),
            [],
        )
    return " ".join(part.get("text", "") for part in parts).strip()


def evaluate(request):
    expected = request["expected"]
    body = request["body"]
    task = task_of(body)
    result = {
        name: score(name, status="UNKNOWN", reason="required evidence missing")
        for name in HARD
    }

    def setscore(name, value=None, **kwargs):
        result[name] = score(name, value, **kwargs)

    def na(name, reason="not required by this Recommendation fixture"):
        setscore(name, status="NOT_APPLICABLE", reason=reason)

    evidence = inspect(body, expected, request.get("message_id"))
    state = task.get("status", {}).get("state")
    complete = state in COMPLETED
    calls = _events(task, "function_call")
    responses = _events(task, "function_response")
    missing_user = expected.get("missing_user") is True
    expected_terminal = complete or (missing_user and state in INPUT_REQUIRED)

    setscore(
        "release_consistency",
        expected_terminal
        and request.get("assigned_release_id") == request.get("release_id")
        and evidence.get("release_id", request.get("release_id"))
        == request.get("release_id"),
        reason="router assignment and immutable adapter release agree",
    )
    dependency_calls = [call for call in calls if call.get("name") == TOOL]
    setscore(
        "duplicate_tool_calls",
        max(0, len(dependency_calls) - (0 if missing_user else 1)),
    )

    if missing_user:
        ask_calls = [call for call in calls if call.get("name") == "ask_user"]
        foreign_calls = [
            call
            for call in calls
            if call.get("name") not in {TOOL, "ask_user"}
            and not native_ask_user_confirmation(call, ask_calls)
        ]
        native_clarification = (
            state in INPUT_REQUIRED
            and len(ask_calls) == 1
            and not dependency_calls
            and not foreign_calls
        )
        terminal_clarification = complete and not calls and not responses
        setscore("trajectory_match", native_clarification or terminal_clarification)
        setscore("missing_user_safe", evidence["verdict"] == "PASS")
        for name in (
            "tool_arguments_match",
            "schema_valid",
            "ranking_preserved",
            "empty_result_correct",
        ):
            na(name)
    else:
        na("missing_user_safe")
        trajectory = (
            complete
            and len(calls) == 1
            and len(responses) == 1
            and calls[0].get("name") == TOOL
            and responses[0].get("name") == TOOL
        )
        setscore("trajectory_match", trajectory)
        setscore(
            "tool_arguments_match",
            len(calls) == 1 and calls[0].get("args") == expected.get("arguments"),
        )
        source = recommendation(responses[0].get("response")) if len(responses) == 1 else None
        rendered = recommendation(
            _answer(task)
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        schema_ok = (
            isinstance(rendered, dict)
            and isinstance(rendered.get("items"), list)
            and isinstance(rendered.get("user_id"), int)
            and isinstance(rendered.get("model_version"), str)
        )
        setscore("schema_valid", schema_ok)
        setscore("ranking_preserved", source is not None and rendered == source)
        if expected.get("empty"):
            setscore(
                "empty_result_correct",
                source is not None
                and rendered == source
                and source.get("items") == []
                and len(calls) == 1,
            )
        else:
            na("empty_result_correct")

    setscore(
        "functional_success",
        evidence["verdict"] == "PASS" if evidence["verdict"] != "HOLD" else None,
        status=evidence["verdict"] if evidence["verdict"] != "HOLD" else "UNKNOWN",
        reason=evidence["reason"],
    )
    setscore(
        "citation_validity",
        status="NOT_APPLICABLE",
        reason="Recommendation-only experiment has no Context/RAG call",
    )
    setscore(
        "retrieval_recall_at_k",
        status="NOT_APPLICABLE",
        reason="Recommendation-only experiment has no retrieval labels",
    )
    for name in ("input_tokens", "output_tokens"):
        value = evidence.get(name)
        known = isinstance(value, int) and value >= 0
        setscore(
            name,
            value if known else None,
            status="OBSERVED" if known else "UNAVAILABLE",
        )
    duration = request.get("duration_seconds")
    known_duration = (
        type(duration) in {int, float}
        and math.isfinite(duration)
        and duration >= 0
    )
    setscore(
        "root_latency_seconds",
        duration if known_duration else None,
        status="OBSERVED" if known_duration else "UNAVAILABLE",
    )
    required = [
        item
        for name, item in result.items()
        if name in HARD and item["status"] != "NOT_APPLICABLE"
    ]
    verdict = (
        "FAIL"
        if any(item["status"] == "FAIL" for item in required)
        else "HOLD"
        if any(item["status"] != "PASS" for item in required)
        else "PASS"
    )
    return {
        "evaluator_version": VERSION,
        "evidence_checksum": digest(request),
        "verdict": verdict,
        "scores": list(result.values()),
    }
