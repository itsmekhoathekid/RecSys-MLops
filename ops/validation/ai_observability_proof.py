#!/usr/bin/env python3
"""Produce a machine-readable production AI observability proof report.

The validator intentionally uses only the Python standard library. It checks
the live Prometheus/Loki/Tempo APIs behind local port-forwards and evaluates
every Prometheus-backed panel in the required provisioned dashboards.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = ROOT / "infra" / "helm" / "recsys-observability" / "dashboards"
REQUIRED_DASHBOARDS = (
    "ai-observability-command-center.json",
    "llm-runtime.json",
    "agent-mcp-operations.json",
    "safety-pii.json",
    "web-api-overview.json",
    "compute-telemetry.json",
    "logs-overview.json",
    "traces-overview.json",
)
SYNTHETIC_VALUES = (
    "observer@example.invalid",
    "+1 202-555-0100",
    "4111111111111111",
)


def fetch_json(
    base: str,
    path: str,
    params: dict[str, str] | None = None,
    *,
    basic_auth: str | None = None,
) -> dict[str, Any]:
    url = f"{base.rstrip('/')}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {"Accept": "application/json"}
    if basic_auth:
        encoded = base64.b64encode(basic_auth.encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=30
    ) as response:
        return json.load(response)


def prometheus_query(base: str, query: str) -> list[dict[str, Any]]:
    payload = fetch_json(base, "/api/v1/query", {"query": query})
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {query}")
    return payload["data"]["result"]


def sample_value(result: list[dict[str, Any]]) -> float:
    if not result:
        return 0.0
    return sum(float(item["value"][1]) for item in result if "value" in item)


def iter_panels(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for panel in panels:
        if panel.get("type") == "row":
            found.extend(iter_panels(panel.get("panels") or []))
        else:
            found.append(panel)
    return found


def resolve_panel_expr(expr: str) -> str:
    replacements = {
        "$namespace": ".*",
        "$agent": ".*",
        "$model": ".*",
        "$tool": ".*",
        "$__rate_interval": "5m",
        "$__interval": "15s",
    }
    for source, target in replacements.items():
        expr = expr.replace(source, target)
    return re.sub(r"\$\{(?:namespace|agent|model|tool):regex\}", ".*", expr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:19090")
    parser.add_argument("--loki-url", default="http://127.0.0.1:13100")
    parser.add_argument("--tempo-url", default="http://127.0.0.1:13200")
    parser.add_argument("--grafana-url", default="http://127.0.0.1:13000")
    parser.add_argument("--grafana-auth", default="admin:admin")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--targets-output", type=Path, required=True)
    args = parser.parse_args()

    timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
    checks: list[dict[str, Any]] = []

    positive_queries = {
        "native_llama": 'count({__name__="llamacpp:prompt_tokens_total"})',
        "native_gateway": "count(agentgateway_requests_total)",
        "web_api": "count(recsys_api_requests_total)",
        "compute": 'count(container_cpu_usage_seconds_total{namespace=~"api-serving|kagent|llm-inference|agentgateway-system"})',
        "mcp_feature": "sum(recsys_mcp_tool_calls_total)",
        "mcp_recommendation": "sum(recsys_recommendation_mcp_tool_calls_total)",
        "mcp_failure": 'sum(recsys_mcp_tool_calls_total{status!="success"})',
        "probe_success": "recsys_llm_probe_success",
        "probe_ttft": "recsys_llm_probe_ttft_seconds",
        "probe_round_trip": "recsys_llm_probe_round_trip_seconds",
        "probe_input_tokens": 'recsys_llm_probe_tokens{type="input"}',
        "probe_output_tokens": 'recsys_llm_probe_tokens{type="output"}',
        "probe_total_tokens": 'recsys_llm_probe_tokens{type="total"}',
        "agent_coordinator": 'sum(recsys_agent_calls_total{agent="coordinator"})',
        "agent_context": 'sum(recsys_agent_calls_total{agent="context"})',
        "agent_recommendation": 'sum(recsys_agent_calls_total{agent="recommendation"})',
        "safety_email": 'sum(recsys_prompt_safety_detections_total{category="email"})',
        "safety_phone": 'sum(recsys_prompt_safety_detections_total{category="phone"})',
        "safety_payment_card": 'sum(recsys_prompt_safety_detections_total{category="payment_card"})',
        "safety_prompt_injection": 'sum(recsys_prompt_safety_detections_total{category="prompt_injection"})',
    }
    for name, query in positive_queries.items():
        result = prometheus_query(args.prometheus_url, query)
        value = sample_value(result)
        checks.append(
            {
                "name": name,
                "source": "prometheus",
                "query": query,
                "timestamp": timestamp,
                "value": value,
                "pass": value > 0,
            }
        )

    target_jobs = (
        "recsys-llama-cpp",
        "recsys-agentgateway-dataplane",
        "recsys-agentgateway-controlplane",
        "recsys-otel-collector",
        "kubernetes-cadvisor",
    )
    for job in target_jobs:
        query = f'min(up{{job="{job}"}})'
        value = sample_value(prometheus_query(args.prometheus_url, query))
        checks.append(
            {
                "name": f"target_{job}",
                "source": "prometheus",
                "query": query,
                "timestamp": timestamp,
                "value": value,
                "pass": value == 1,
            }
        )

    unknown_query = 'sum(recsys_agent_calls_total{agent="unknown"})'
    unknown = sample_value(prometheus_query(args.prometheus_url, unknown_query))
    checks.append(
        {
            "name": "agent_unknown_absent",
            "source": "prometheus",
            "query": unknown_query,
            "timestamp": timestamp,
            "value": unknown,
            "pass": unknown == 0,
        }
    )

    for raw_value in SYNTHETIC_VALUES:
        # Acceptance is evaluated over the post-traffic 90-second proof window.
        # Include Loki itself so its access logs cannot reintroduce a literal
        # fixture after the Promtail redaction pipeline has processed them.
        query = (
            'sum(count_over_time({namespace=~"kagent|observability|llm-inference"}'
            f' |= {json.dumps(raw_value)} [90s]))'
        )
        result = fetch_json(args.loki_url, "/loki/api/v1/query", {"query": query})
        value = sample_value(result["data"]["result"])
        checks.append(
            {
                "name": f"loki_redacted_{SYNTHETIC_VALUES.index(raw_value)}",
                "source": "loki",
                "query": query.replace(raw_value, "[REDACTED_FIXTURE]"),
                "timestamp": timestamp,
                "value": value,
                "pass": value == 0,
            }
        )

    tempo_search = fetch_json(
        args.tempo_url,
        "/api/search",
        {
            "q": '{ resource.service.name =~ ".*context.*|.*recommendation.*|.*coordinator.*" }',
            "limit": "50",
        },
    )
    traces = tempo_search.get("traces") or []
    serialized_traces: list[str] = []
    for item in traces:
        trace_id = item.get("traceID")
        if trace_id:
            serialized_traces.append(
                json.dumps(fetch_json(args.tempo_url, f"/api/traces/{trace_id}"))
            )
    raw_in_tempo = [
        value for value in SYNTHETIC_VALUES if any(value in trace for trace in serialized_traces)
    ]
    checks.extend(
        [
            {
                "name": "tempo_kagent_traces",
                "source": "tempo",
                "query": "kagent service-name search",
                "timestamp": timestamp,
                "value": len(traces),
                "pass": len(traces) > 0,
            },
            {
                "name": "tempo_raw_pii_absent",
                "source": "tempo",
                "query": "downloaded matching traces scanned in memory",
                "timestamp": timestamp,
                "value": len(raw_in_tempo),
                "pass": not raw_in_tempo,
            },
        ]
    )

    grafana_dashboards = fetch_json(
        args.grafana_url,
        "/api/search",
        {"type": "dash-db"},
        basic_auth=args.grafana_auth,
    )
    provisioned_uids = {item.get("uid") for item in grafana_dashboards}
    panel_checks: list[dict[str, Any]] = []
    for filename in REQUIRED_DASHBOARDS:
        dashboard = json.loads((DASHBOARD_DIR / filename).read_text())
        checks.append(
            {
                "name": f"dashboard_{dashboard['uid']}_provisioned",
                "source": "grafana",
                "query": dashboard["uid"],
                "timestamp": timestamp,
                "value": 1 if dashboard["uid"] in provisioned_uids else 0,
                "pass": dashboard["uid"] in provisioned_uids,
            }
        )
        for panel in iter_panels(dashboard.get("panels") or []):
            datasource = panel.get("datasource") or {}
            if datasource.get("type") != "prometheus":
                continue
            for target in panel.get("targets") or []:
                expr = target.get("expr")
                if not expr:
                    continue
                resolved = resolve_panel_expr(expr)
                try:
                    result = prometheus_query(args.prometheus_url, resolved)
                    passed = bool(result)
                    error = None
                except Exception as exc:  # keep every failing panel in evidence
                    result = []
                    passed = False
                    error = str(exc)
                panel_checks.append(
                    {
                        "dashboard": dashboard["uid"],
                        "panel_id": panel.get("id"),
                        "panel": panel.get("title"),
                        "query": resolved,
                        "series": len(result),
                        "pass": passed,
                        "error": error,
                    }
                )

    target_snapshot = fetch_json(
        args.prometheus_url, "/api/v1/targets", {"state": "active"}
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.targets_output.parent.mkdir(parents=True, exist_ok=True)
    args.targets_output.write_text(json.dumps(target_snapshot, indent=2, sort_keys=True))

    passed = all(check["pass"] for check in checks) and all(
        check["pass"] for check in panel_checks
    )
    report = {
        "generated_at": timestamp,
        "production_context": "recsys-mlops-gke",
        "pass": passed,
        "checks": checks,
        "prometheus_panel_checks": panel_checks,
        "summary": {
            "checks_passed": sum(bool(item["pass"]) for item in checks),
            "checks_total": len(checks),
            "panels_passed": sum(bool(item["pass"]) for item in panel_checks),
            "panels_total": len(panel_checks),
        },
    }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report["summary"] | {"pass": passed}, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
