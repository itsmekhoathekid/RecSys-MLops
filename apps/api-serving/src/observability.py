from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "recsys-api-serving")


def _label_key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((labels or {}).items()))


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    payload = ",".join(f'{key}="{value}"' for key, value in labels)
    return "{" + payload + "}"


@dataclass
class MetricsStore:
    counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = field(default_factory=dict)
    gauges: dict[str, dict[tuple[tuple[str, str], ...], float]] = field(default_factory=dict)
    summaries: dict[str, dict[tuple[tuple[str, str], ...], dict[str, float]]] = field(default_factory=dict)
    histograms: dict[str, dict[tuple[tuple[str, str], ...], dict[str, Any]]] = field(default_factory=dict)

    def inc(self, name: str, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        series = self.counters.setdefault(name, {})
        key = _label_key(labels)
        series[key] = series.get(key, 0.0) + value

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        self.gauges.setdefault(name, {})[_label_key(labels)] = value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        series = self.summaries.setdefault(name, {})
        bucket = series.setdefault(_label_key(labels), {"count": 0.0, "sum": 0.0, "max": 0.0})
        bucket["count"] += 1.0
        bucket["sum"] += value
        bucket["max"] = max(bucket["max"], value)

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
        buckets: tuple[float, ...] = (),
    ) -> None:
        series = self.histograms.setdefault(name, {})
        key = _label_key(labels)
        bucket = series.setdefault(
            key,
            {"buckets": {boundary: 0.0 for boundary in buckets}, "count": 0.0, "sum": 0.0},
        )
        bucket["count"] += 1.0
        bucket["sum"] += value
        for boundary in buckets:
            if value <= boundary:
                bucket["buckets"][boundary] += 1.0

    def render(self) -> str:
        lines = [
            "# HELP recsys_observability_build_info RecSys observability build info",
            "# TYPE recsys_observability_build_info gauge",
            f'recsys_observability_build_info{{service="{SERVICE_NAME}"}} 1',
        ]
        for name in sorted(self.counters):
            lines.append(f"# TYPE {name} counter")
            for labels, value in sorted(self.counters[name].items()):
                lines.append(f"{name}{_format_labels(labels)} {value}")
        for name in sorted(self.gauges):
            lines.append(f"# TYPE {name} gauge")
            for labels, value in sorted(self.gauges[name].items()):
                lines.append(f"{name}{_format_labels(labels)} {value}")
        for name in sorted(self.summaries):
            lines.append(f"# TYPE {name} summary")
            for labels, values in sorted(self.summaries[name].items()):
                rendered = _format_labels(labels)
                lines.append(f"{name}_count{rendered} {values['count']}")
                lines.append(f"{name}_sum{rendered} {values['sum']}")
                lines.append(f"{name}_max{rendered} {values['max']}")
        for name in sorted(self.histograms):
            lines.append(f"# TYPE {name} histogram")
            for labels, values in sorted(self.histograms[name].items()):
                for boundary, count in sorted(values["buckets"].items()):
                    bucket_labels = dict(labels)
                    bucket_labels["le"] = str(boundary)
                    lines.append(f"{name}_bucket{_format_labels(_label_key(bucket_labels))} {count}")
                inf_labels = dict(labels)
                inf_labels["le"] = "+Inf"
                lines.append(f"{name}_bucket{_format_labels(_label_key(inf_labels))} {values['count']}")
                rendered = _format_labels(labels)
                lines.append(f"{name}_count{rendered} {values['count']}")
                lines.append(f"{name}_sum{rendered} {values['sum']}")
        return "\n".join(lines) + "\n"


METRICS = MetricsStore()
LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1.0, 2.5, 5.0)
CONFIDENCE_BUCKETS = (0.0, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 1.0)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "message": record.getMessage(),
        }
        for key in ["component", "route", "method", "status", "duration_ms", "model_version", "error_type"]:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        trace_id, span_id = current_trace_context()
        if trace_id:
            payload["trace_id"] = trace_id
        if span_id:
            payload["span_id"] = span_id
        return json.dumps(payload, sort_keys=True)


def configure_logging() -> None:
    if os.getenv("RECSYS_JSON_LOGS", "1") != "1":
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))


def log_event(message: str, level: int = logging.INFO, **fields: Any) -> None:
    logging.getLogger(SERVICE_NAME).log(level, message, extra=fields)


def configure_tracing(app: Any) -> None:
    if os.getenv("RECSYS_OTEL_ENABLED", "1") != "1":
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception:
        return

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer(SERVICE_NAME)
        with tracer.start_as_current_span(name) as active_span:
            for key, value in attributes.items():
                active_span.set_attribute(key, value)
            yield
    except Exception:
        yield


def current_trace_context() -> tuple[str | None, str | None]:
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return None, None
        return f"{context.trace_id:032x}", f"{context.span_id:016x}"
    except Exception:
        return None, None


@contextmanager
def timed_operation(metric: str, labels: dict[str, str] | None = None) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        METRICS.observe(metric, time.perf_counter() - start, labels)


def observe_request(route: str, method: str, status: int, duration_seconds: float) -> None:
    labels = {"route": route, "method": method, "status": str(status)}
    METRICS.inc("recsys_api_requests_total", labels=labels)
    if status >= 500:
        METRICS.inc("recsys_api_failures_total", labels={"route": route, "method": method, "status": str(status)})
    METRICS.observe("recsys_api_request_duration_seconds", duration_seconds, {"route": route, "method": method})


def observe_redis(operation: str, duration_seconds: float, error: bool = False) -> None:
    METRICS.observe("recsys_api_redis_operation_duration_seconds", duration_seconds, {"operation": operation})
    if error:
        METRICS.inc("recsys_api_redis_errors_total", labels={"operation": operation})


def observe_triton(
    model_name: str,
    duration_seconds: float,
    error: bool = False,
    labels: dict[str, str] | None = None,
) -> None:
    metric_labels = {"model_name": model_name}
    metric_labels.update(labels or {})
    METRICS.observe("recsys_api_triton_inference_duration_seconds", duration_seconds, metric_labels)
    if error:
        METRICS.inc("recsys_api_triton_errors_total", labels=metric_labels)


def observe_model_prediction(
    model_version: str,
    duration_seconds: float,
    confidence: float | None,
    status: str,
    labels: dict[str, str] | None = None,
) -> None:
    metric_labels = {"model_version": model_version, "status": status}
    metric_labels.update(labels or {})
    METRICS.inc("model_predictions_total", labels=metric_labels)
    histogram_labels = {"model_version": model_version}
    histogram_labels.update(labels or {})
    METRICS.observe_histogram(
        "model_prediction_latency_seconds",
        duration_seconds,
        labels=histogram_labels,
        buckets=LATENCY_BUCKETS,
    )
    if confidence is not None:
        METRICS.observe_histogram(
            "model_prediction_confidence",
            max(0.0, min(1.0, confidence)),
            labels=histogram_labels,
            buckets=CONFIDENCE_BUCKETS,
        )


def metrics_text() -> str:
    return METRICS.render()
