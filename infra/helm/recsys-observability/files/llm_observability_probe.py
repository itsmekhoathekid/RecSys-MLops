"""Low-volume streaming probe for production LLM observability.

The implementation intentionally uses only the Python standard library so the
CronJob can reuse the repository's data-ingestion runtime image.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable


@dataclass(frozen=True)
class ProbeResult:
    success: bool
    ttft_seconds: float
    round_trip_seconds: float
    input_tokens: int
    output_tokens: int
    total_tokens: int


class ProbeError(RuntimeError):
    """Raised when a response cannot produce trustworthy probe metrics."""


def parse_sse(
    lines: Iterable[bytes | str],
    *,
    started_at: float,
    clock: Callable[[], float] = time.perf_counter,
) -> ProbeResult:
    """Parse OpenAI-compatible SSE and measure the first non-empty content token."""

    ttft: float | None = None
    usage: dict[str, int] | None = None
    saw_done = False

    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="strict") if isinstance(raw_line, bytes) else raw_line
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            saw_done = True
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProbeError(f"malformed SSE JSON: {exc}") from exc

        event_usage = event.get("usage")
        if isinstance(event_usage, dict):
            usage = event_usage

        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            # Tool-call/reasoning chunks do not count as time-to-first-content.
            if ttft is None and isinstance(content, str) and content:
                ttft = max(clock() - started_at, 0.0)

    round_trip = max(clock() - started_at, 0.0)
    if not saw_done:
        raise ProbeError("stream ended without [DONE]")
    if ttft is None:
        raise ProbeError("[DONE] received without a content token")
    if usage is None:
        raise ProbeError("stream did not include usage")

    try:
        input_tokens = int(usage["prompt_tokens"])
        output_tokens = int(usage["completion_tokens"])
        total_tokens = int(usage["total_tokens"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbeError("usage is missing integer token fields") from exc
    if min(input_tokens, output_tokens, total_tokens) < 0:
        raise ProbeError("usage contains negative tokens")

    return ProbeResult(True, ttft, round_trip, input_tokens, output_tokens, total_tokens)


def render_metrics(result: ProbeResult, *, timestamp: float) -> bytes:
    """Render a stable Pushgateway metric group without request-shaped labels."""

    values = {
        "input": result.input_tokens,
        "output": result.output_tokens,
        "total": result.total_tokens,
    }
    lines = [
        "# TYPE recsys_llm_probe_success gauge",
        f"recsys_llm_probe_success {1 if result.success else 0}",
        "# TYPE recsys_llm_probe_ttft_seconds gauge",
        f"recsys_llm_probe_ttft_seconds {result.ttft_seconds:.9f}",
        "# TYPE recsys_llm_probe_round_trip_seconds gauge",
        f"recsys_llm_probe_round_trip_seconds {result.round_trip_seconds:.9f}",
        "# TYPE recsys_llm_probe_tokens gauge",
    ]
    lines.extend(f'recsys_llm_probe_tokens{{type="{kind}"}} {value}' for kind, value in values.items())
    lines.extend(
        [
            "# TYPE recsys_llm_probe_last_success_timestamp_seconds gauge",
            f"recsys_llm_probe_last_success_timestamp_seconds {timestamp:.3f}",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def push_metrics(pushgateway_url: str, result: ProbeResult) -> None:
    timestamp = time.time() if result.success else 0.0
    request = urllib.request.Request(
        pushgateway_url,
        data=render_metrics(result, timestamp=timestamp),
        method="PUT",
        headers={"Content-Type": "text/plain; version=0.0.4"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status // 100 != 2:
            raise ProbeError(f"Pushgateway returned HTTP {response.status}")


def _otlp_attribute(key: str, value: str | int | bool) -> dict[str, object]:
    if isinstance(value, bool):
        encoded: dict[str, object] = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}
    else:
        encoded = {"stringValue": value}
    return {"key": key, "value": encoded}


def render_otlp_trace(
    result: ProbeResult,
    *,
    model: str,
    ended_at_ns: int | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> bytes:
    """Render one immutable v4-ready generation span for Langfuse fan-out."""

    ended_at_ns = ended_at_ns or time.time_ns()
    start_time_ns = max(ended_at_ns - int(result.round_trip_seconds * 1e9), 0)
    completion_time_ns = start_time_ns + int(result.ttft_seconds * 1e9)
    completion_time = datetime.fromtimestamp(
        completion_time_ns / 1e9, tz=timezone.utc
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    usage = json.dumps(
        {
            "input": result.input_tokens,
            "output": result.output_tokens,
            "total": result.total_tokens,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    attributes = [
        _otlp_attribute("langfuse.trace.name", "recsys-synthetic-llm-probe"),
        _otlp_attribute("langfuse.environment", "production"),
        _otlp_attribute("langfuse.observation.type", "generation"),
        _otlp_attribute("langfuse.observation.model.name", model),
        _otlp_attribute("langfuse.observation.input", "Reply with exactly READY"),
        _otlp_attribute(
            "langfuse.observation.output", "READY" if result.success else "probe failed"
        ),
        _otlp_attribute("langfuse.observation.usage_details", usage),
        _otlp_attribute("langfuse.observation.completion_start_time", completion_time),
        _otlp_attribute("langfuse.observation.metadata.probe", "streaming"),
        _otlp_attribute("gen_ai.request.model", model),
        _otlp_attribute("gen_ai.usage.input_tokens", result.input_tokens),
        _otlp_attribute("gen_ai.usage.output_tokens", result.output_tokens),
        _otlp_attribute("gen_ai.usage.total_tokens", result.total_tokens),
    ]
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _otlp_attribute("service.name", "recsys-llm-observability-probe"),
                        _otlp_attribute("service.namespace", "observability"),
                        _otlp_attribute("deployment.environment.name", "production"),
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "recsys.synthetic-probe", "version": "1.1"},
                        "spans": [
                            {
                                "traceId": trace_id or secrets.token_hex(16),
                                "spanId": span_id or secrets.token_hex(8),
                                "name": "synthetic probe generation",
                                "kind": 3,
                                "startTimeUnixNano": str(start_time_ns),
                                "endTimeUnixNano": str(ended_at_ns),
                                "attributes": attributes,
                                "status": {
                                    "code": 1 if result.success else 2,
                                    "message": "success" if result.success else "probe failed",
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def export_otlp_trace(otlp_endpoint: str, result: ProbeResult, *, model: str) -> None:
    request = urllib.request.Request(
        otlp_endpoint,
        data=render_otlp_trace(result, model=model),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status // 100 != 2:
            raise ProbeError(f"OTLP Collector returned HTTP {response.status}")


def run_probe() -> ProbeResult:
    gateway_url = os.environ["LLM_GATEWAY_URL"]
    api_key = os.environ["AGENT_GATEWAY_API_KEY"]
    model = os.environ.get("LLM_MODEL", "qwen3.5-0.8b")
    base_model = os.environ.get("LLM_BASE_MODEL", "llm-d-optimized-baseline")
    max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "8"))
    timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "240"))
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly READY"}],
            "max_tokens": max_tokens,
            "temperature": 0,
            # The production Qwen template otherwise consumes the eight-token
            # probe budget as hidden reasoning and may never emit content.
            # This flag only affects the synthetic request and keeps TTFT tied
            # to the first user-visible token.
            "chat_template_kwargs": {"enable_thinking": False},
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        gateway_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Gateway-Base-Model-Name": base_model,
            "User-Agent": "recsys-llm-observability-probe/1.0",
        },
    )
    started_at = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status // 100 != 2:
                raise ProbeError(f"gateway returned HTTP {response.status}")
            return parse_sse(response, started_at=started_at)
    except urllib.error.HTTPError as exc:
        raise ProbeError(f"gateway returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProbeError(f"gateway request failed: {exc}") from exc


def main() -> int:
    pushgateway_url = os.environ.get(
        "PUSHGATEWAY_URL",
        "http://recsys-pushgateway.observability.svc.cluster.local:9091/metrics/job/recsys-llm-observability-probe/instance/production",
    )
    try:
        result = run_probe()
    except Exception as exc:  # failure must still be observable
        result = ProbeResult(False, 0.0, 0.0, 0, 0, 0)
        print(f"probe failed: {exc}", file=sys.stderr)
    push_metrics(pushgateway_url, result)
    export_otlp_trace(
        os.environ.get(
            "OTEL_TRACES_URL",
            "http://recsys-otel-collector.observability.svc.cluster.local:4318/v1/traces",
        ),
        result,
        model=os.environ.get("LLM_MODEL", "qwen3.5-0.8b"),
    )
    print(json.dumps(result.__dict__, sort_keys=True))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
