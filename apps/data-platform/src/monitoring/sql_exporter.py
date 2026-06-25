from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from warehouse.connection import connect
from warehouse.writer import ensure_warehouse


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _metric(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    label_text = ""
    if labels:
        rendered = ",".join(
            f'{key}="{_escape_label_value(str(value))}"'
            for key, value in sorted(labels.items())
        )
        label_text = "{" + rendered + "}"
    return f"{name}{label_text} {float(value)}"


def _query_rows(sql: str) -> list[dict[str, Any]]:
    with connect() as connection:
        cursor = connection.cursor()
        cursor.execute(sql)
        columns = [column.name for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _flatten_numeric_metrics(payload: Any, prefix: str = "") -> dict[str, float]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return {}
    flattened: dict[str, float] = {}
    for key, value in payload.items():
        metric_key = f"{prefix}_{key}" if prefix else str(key)
        metric_key = "".join(
            character if character.isalnum() or character == "_" else "_"
            for character in metric_key
        )
        if isinstance(value, bool):
            flattened[metric_key] = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            flattened[metric_key] = float(value)
        elif isinstance(value, dict):
            flattened.update(_flatten_numeric_metrics(value, metric_key))
    return flattened


def _ensure_monitoring_tables() -> None:
    with connect() as connection:
        ensure_warehouse(connection)


def collect_metrics() -> str:
    lines = ["# TYPE recsys_monitoring_exporter_up gauge", "recsys_monitoring_exporter_up 1"]
    try:
        _ensure_monitoring_tables()
        for row in _query_rows(
            """
            SELECT window_start, window_end, topic, event_count, late_event_count, duplicate_event_count, max_late_by_seconds, is_bursty
            FROM monitoring.streaming_quality_windows
            ORDER BY window_end DESC
            LIMIT 50
            """
        ):
            labels = {
                "topic": str(row["topic"]),
                "window_start": str(row["window_start"]),
                "window_end": str(row["window_end"]),
            }
            lines.append(_metric("recsys_streaming_event_count", row["event_count"], labels))
            lines.append(_metric("recsys_streaming_late_event_count", row["late_event_count"], labels))
            lines.append(_metric("recsys_streaming_duplicate_event_count", row["duplicate_event_count"], labels))
            lines.append(_metric("recsys_streaming_max_late_by_seconds", row["max_late_by_seconds"], labels))
            lines.append(_metric("recsys_streaming_bursty_window", 1.0 if row["is_bursty"] else 0.0, labels))
        for row in _query_rows(
            """
            SELECT run_id, check_name, passed, error_count, metrics
            FROM monitoring.data_quality_runs
            ORDER BY created_timestamp DESC
            LIMIT 50
            """
        ):
            labels = {"run_id": str(row["run_id"]), "check_name": str(row["check_name"])}
            lines.append(_metric("recsys_data_quality_passed", 1.0 if row["passed"] else 0.0, labels))
            lines.append(_metric("recsys_data_quality_error_count", row["error_count"], labels))
            for metric_name, metric_value in _flatten_numeric_metrics(row["metrics"]).items():
                lines.append(
                    _metric(
                        "recsys_data_quality_metric",
                        metric_value,
                        {
                            "run_id": str(row["run_id"]),
                            "check_name": str(row["check_name"]),
                            "metric_name": metric_name,
                        },
                    )
                )
        for row in _query_rows(
            """
            SELECT feature_name, drift_score, passed
            FROM monitoring.feature_drift_runs
            ORDER BY created_timestamp DESC
            LIMIT 100
            """
        ):
            labels = {"feature": str(row["feature_name"])}
            lines.append(_metric("recsys_feature_drift_score", row["drift_score"], labels))
            lines.append(_metric("recsys_feature_drift_passed", 1.0 if row["passed"] else 0.0, labels))
        for row in _query_rows(
            """
            SELECT feature_view, scanned_rows, synced_rows, skipped_rows
            FROM monitoring.online_store_sync_runs
            ORDER BY created_timestamp DESC
            LIMIT 50
            """
        ):
            labels = {"feature_view": str(row["feature_view"])}
            lines.append(_metric("recsys_online_store_sync_scanned_rows", row["scanned_rows"], labels))
            lines.append(_metric("recsys_online_store_sync_synced_rows", row["synced_rows"], labels))
            lines.append(_metric("recsys_online_store_sync_skipped_rows", row["skipped_rows"], labels))
    except Exception as exc:
        lines.append("recsys_monitoring_exporter_up 0")
        lines.append(_metric("recsys_monitoring_exporter_error", 1.0, {"error_type": exc.__class__.__name__}))
    return "\n".join(lines) + "\n"


def _http_port() -> int:
    for name in ("SQL_EXPORTER_HTTP_PORT", "MONITORING_SQL_EXPORTER_PORT"):
        value = os.getenv(name)
        if value and value.isdigit():
            return int(value)
    return 9102


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in {"/metrics", "/"}:
            self.send_response(404)
            self.end_headers()
            return
        payload = collect_metrics().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> int:
    port = _http_port()
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
