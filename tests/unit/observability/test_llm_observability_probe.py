from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
PROBE_PATH = (
    ROOT
    / "infra"
    / "helm"
    / "recsys-observability"
    / "files"
    / "llm_observability_probe.py"
)
SPEC = importlib.util.spec_from_file_location("llm_observability_probe", PROBE_PATH)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


class Clock:
    def __init__(self, *values: float) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        return next(self.values)


def test_normal_stream_records_exact_tokens_and_ttft() -> None:
    lines = [
        'data: {"choices":[{"delta":{"content":"READY"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}',
        "data: [DONE]",
    ]
    result = probe.parse_sse(lines, started_at=100.0, clock=Clock(101.25, 102.0))
    assert result == probe.ProbeResult(True, 1.25, 2.0, 10, 2, 12)


def test_tool_call_chunk_does_not_count_as_first_content() -> None:
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"call-1"}]}}]}',
        'data: {"choices":[{"delta":{"content":"READY"}}]}',
        'data: {"usage":{"prompt_tokens":8,"completion_tokens":1,"total_tokens":9}}',
        "data: [DONE]",
    ]
    result = probe.parse_sse(lines, started_at=10.0, clock=Clock(12.0, 13.0))
    assert result.ttft_seconds == 2.0


@pytest.mark.parametrize(
    "lines,match",
    [
        (["data: {not-json}", "data: [DONE]"], "malformed"),
        (
            ['data: {"choices":[{"delta":{"content":"READY"}}]}', "data: [DONE]"],
            "usage",
        ),
        (
            [
                'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}',
                "data: [DONE]",
            ],
            "without a content token",
        ),
        (
            [
                'data: {"choices":[{"delta":{"content":"READY"}}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'
            ],
            r"without \[DONE\]",
        ),
    ],
)
def test_invalid_streams_fail_closed(lines: list[str], match: str) -> None:
    with pytest.raises(probe.ProbeError, match=match):
        probe.parse_sse(lines, started_at=0.0, clock=Clock(1.0, 2.0))


def test_timeout_and_http_failures_are_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://gateway.invalid/v1/chat/completions")
    monkeypatch.setenv("AGENT_GATEWAY_API_KEY", "test-only")

    monkeypatch.setattr(probe.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("timeout")))
    with pytest.raises(probe.ProbeError, match="gateway request failed"):
        probe.run_probe()

    monkeypatch.setattr(
        probe.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.HTTPError("http://gateway.invalid", 503, "unavailable", {}, None)
        ),
    )
    with pytest.raises(probe.ProbeError, match="HTTP 503"):
        probe.run_probe()


def test_probe_disables_hidden_reasoning_within_eight_token_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://gateway.invalid/v1/chat/completions")
    monkeypatch.setenv("AGENT_GATEWAY_API_KEY", "test-only")
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, **_kwargs: object) -> object:
        captured["body"] = json.loads(request.data)  # type: ignore[attr-defined]
        raise TimeoutError("stop after request capture")

    monkeypatch.setattr(probe.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(probe.ProbeError, match="gateway request failed"):
        probe.run_probe()
    assert captured["body"]["max_tokens"] == 8  # type: ignore[index]
    assert captured["body"]["chat_template_kwargs"] == {  # type: ignore[index]
        "enable_thinking": False
    }


def test_metrics_have_fixed_low_cardinality_contract() -> None:
    payload = probe.render_metrics(
        probe.ProbeResult(True, 0.5, 1.5, 10, 2, 12), timestamp=123.0
    ).decode()
    assert 'recsys_llm_probe_tokens{type="input"} 10' in payload
    assert 'recsys_llm_probe_tokens{type="output"} 2' in payload
    assert 'recsys_llm_probe_tokens{type="total"} 12' in payload
    assert "request_id" not in payload
    assert "test-only-secret" not in payload


def test_otlp_trace_contains_exact_generation_timing_and_usage() -> None:
    payload = json.loads(
        probe.render_otlp_trace(
            probe.ProbeResult(True, 0.5, 1.5, 10, 2, 12),
            model="qwen-test",
            ended_at_ns=2_000_000_000,
            trace_id="0" * 32,
            span_id="1" * 16,
        )
    )
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["startTimeUnixNano"] == "500000000"
    assert span["endTimeUnixNano"] == "2000000000"
    attributes = {
        item["key"]: next(iter(item["value"].values()))
        for item in span["attributes"]
    }
    assert attributes["langfuse.observation.type"] == "generation"
    assert attributes["langfuse.observation.model.name"] == "qwen-test"
    assert json.loads(attributes["langfuse.observation.usage_details"]) == {
        "input": 10,
        "output": 2,
        "total": 12,
    }
    assert attributes["gen_ai.usage.total_tokens"] == "12"
    serialized = json.dumps(payload)
    assert "AGENT_GATEWAY_API_KEY" not in serialized
    assert "Authorization" not in serialized
