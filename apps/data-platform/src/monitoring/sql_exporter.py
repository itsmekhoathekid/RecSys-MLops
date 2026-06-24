from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from warehouse.connection import connect


def _metric(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    label_text = ""
    if labels:
        rendered = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
        label_text = "{" + rendered + "}"
    return f"{name}{label_text} {float(value)}"


def _query_rows(sql: str) -> list[dict[str, Any]]:
    with connect() as connection:
        cursor = connection.cursor()
        cursor.execute(sql)
        columns = [column.name for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def collect_metrics() -> str:
    lines = ["# TYPE recsys_monitoring_exporter_up gauge", "recsys_monitoring_exporter_up 1"]
    try:
        for row in _query_rows(
            """
            SELECT topic, event_count, late_event_count, max_late_by_seconds, is_bursty
            FROM monitoring.streaming_quality_windows
            ORDER BY window_end DESC
            LIMIT 50
            """
        ):
            labels = {"topic": str(row["topic"])}
            lines.append(_metric("recsys_streaming_event_count", row["event_count"], labels))
            lines.append(_metric("recsys_streaming_late_event_count", row["late_event_count"], labels))
            lines.append(_metric("recsys_streaming_max_late_by_seconds", row["max_late_by_seconds"], labels))
            lines.append(_metric("recsys_streaming_bursty_window", 1.0 if row["is_bursty"] else 0.0, labels))
        for row in _query_rows(
            """
            SELECT check_name, passed, error_count
            FROM monitoring.data_quality_runs
            ORDER BY created_timestamp DESC
            LIMIT 50
            """
        ):
            labels = {"check_name": str(row["check_name"])}
            lines.append(_metric("recsys_data_quality_passed", 1.0 if row["passed"] else 0.0, labels))
            lines.append(_metric("recsys_data_quality_error_count", row["error_count"], labels))
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
