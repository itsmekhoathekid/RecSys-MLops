"""Deterministic Grafana dashboard source; stdout is the provisioned JSON.

Keep the Final ML dashboard untouched. This dashboard keeps the public UID
``recsys-llm-ab`` while presenting the workflow experiment as a compact control
room. DB snapshot series are HA-deduplicated before aggregation and closed gate
windows remain immutable after promotion.
"""
import json

PROM = {"type": "prometheus", "uid": "Prometheus"}
LOKI = {"type": "loki", "uid": "Loki"}
E = 'experiment_id=~"$experiment"'
S = E + ',source=~"$source",phase=~"$phase"'


def build():
    panels = []
    branch_and_status_overrides = [
        {"matcher": {"id": "byRegexp", "options": ".*control.*"},
         "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "blue"}}]},
        {"matcher": {"id": "byRegexp", "options": ".*candidate.*"},
         "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}]},
        {"matcher": {"id": "byRegexp", "options": ".*PASS.*|.*COMPLETED.*"},
         "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "green"}}]},
        {"matcher": {"id": "byRegexp", "options": ".*HOLD.*|.*UNKNOWN.*"},
         "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "yellow"}}]},
        {"matcher": {"id": "byRegexp", "options": ".*FAIL.*|.*ROLLED_BACK.*|.*ROLLBACK_FAILED.*"},
         "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]},
        {"matcher": {"id": "byRegexp", "options": ".*UNAVAILABLE.*|.*NOT_APPLICABLE.*"},
         "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "gray"}}]},
    ]

    def panel(pid, title, expressions=None, *, kind="timeseries", unit="short",
              text=None, description="", nodata="UNKNOWN / UNAVAILABLE",
              datasource=PROM, text_mode=None, thresholds=None, min_value=None,
              max_value=None, color=None, mappings=None):
        defaults = {"unit": unit, "noValue": nodata,
                    "color": ({"mode": "fixed", "fixedColor": color}
                              if color else {"mode": "palette-classic"})}
        if thresholds:
            defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
            # Grafana otherwise keeps the palette-classic series colour and
            # merely draws threshold markers. Gate cards must colour the value
            # from the frozen policy threshold itself.
            if not color:
                defaults["color"] = {"mode": "thresholds"}
        if min_value is not None:
            defaults["min"] = min_value
        if max_value is not None:
            defaults["max"] = max_value
        if mappings:
            defaults["mappings"] = mappings
        result = {
            "id": pid, "title": title, "type": kind,
            "description": description, "datasource": datasource,
            "fieldConfig": {"defaults": defaults,
                            "overrides": list(branch_and_status_overrides)},
            "targets": [],
        }
        instant_kinds = {"stat", "gauge", "bargauge", "table", "piechart"}
        for index, (legend, expression) in enumerate((expressions or {}).items()):
            target = {
                "refId": chr(65 + index), "expr": expression,
                "legendFormat": legend,
                "instant": kind in instant_kinds and datasource == PROM,
                "format": "table" if kind == "table" and datasource == PROM else "time_series",
            }
            # These panels render state transitions, not dense telemetry. A
            # 24-hour dashboard at the 15-second scrape interval otherwise
            # exceeds Grafana's discrete-state point limit even though every
            # series is constant between transitions.
            if kind in {"status-history", "state-timeline"}:
                target["maxDataPoints"] = 100
                target["interval"] = "1h"
            result["targets"].append(target)
        if kind == "text":
            result["options"] = {"mode": "markdown", "content": text}
            result["transparent"] = True
        elif kind == "stat":
            result["options"] = {
                "colorMode": "background" if text_mode == "name" else "value",
                "graphMode": "none", "justifyMode": "center", "orientation": "horizontal",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "textMode": text_mode or "auto",
            }
        elif kind == "gauge":
            result["options"] = {
                "orientation": "auto",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "showThresholdLabels": True, "showThresholdMarkers": True, "sizing": "auto",
            }
        elif kind == "bargauge":
            result["options"] = {
                "displayMode": "gradient", "orientation": "horizontal",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "showUnfilled": True, "sizing": "auto",
            }
        elif kind == "piechart":
            result["options"] = {
                # Keep each donut to one question and a small number of slices.
                "pieType": "donut", "displayLabels": [],
                "legend": {"showLegend": True, "displayMode": "table", "placement": "right", "values": ["value", "percent"]},
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "tooltip": {"mode": "multi", "sort": "desc"},
            }
        elif kind == "timeseries":
            result["fieldConfig"]["defaults"]["custom"] = {
                "drawStyle": "line", "lineInterpolation": "smooth", "lineWidth": 2,
                "fillOpacity": 12, "showPoints": "never", "spanNulls": False,
            }
            result["options"] = {
                # A list legend takes materially less vertical space than the
                # default Last/Max table and does not cover short comparison
                # charts when source=All expands to several series.
                "legend": {"displayMode": "list", "placement": "bottom",
                           "calcs": []},
                "tooltip": {"mode": "multi", "sort": "desc"},
            }
        elif kind == "table":
            result["options"] = {"showHeader": True, "cellHeight": "sm"}
            if datasource == LOKI:
                group_key = "case_id" if "ab.case" in str(expressions) else "event_id"
                columns = (
                    "experiment_id", "source", "phase", "case_id", "group", "variant", "release_id",
                    "verdict", "reason", "duration_seconds", "trace_id", "build_url", "policy_checksum",
                    "role", "before", "after", "overrides", "changed", "start", "end", "observation",
                    "latency_ratio", "at", "route_revision", "verified_weight", "status", "queue_id",
                    "job_name", "metric", "score_status", "value", "required", "evaluator_version",
                    "evidence_checksum", "synced", "fixture_checksum",
                )
                result["transformations"] = [
                    {"id": "sortBy", "options": {"sort": [{"field": "Time", "desc": False}]}},
                    {"id": "extractFields", "options": {"source": "Line", "format": "json", "replace": True}},
                    {"id": "groupBy", "options": {"fields": {
                        **{name: {"operation": "aggregate", "aggregations": ["lastNotNull"]}
                           for name in columns if name != group_key},
                        group_key: {"operation": "groupby", "aggregations": []},
                    }}},
                ]
            else:
                # Prometheus instant tables contain implementation columns that
                # do not help a release decision. Keep only the human-readable
                # case dimensions; the metric value is always 1 for a result row.
                result["transformations"] = [{
                    "id": "organize",
                    "options": {
                        "excludeByName": {
                            "Time": True, "Value": True, "__name__": True,
                            "experiment_id": True,
                        },
                        "indexByName": {
                            "case_id": 0, "group": 1, "variant": 2, "verdict": 3,
                        },
                        "renameByName": {
                            "case_id": "Case", "group": "Group",
                            "variant": "Variant", "verdict": "Verdict",
                        },
                    },
                }]
        elif kind == "status-history":
            result["options"] = {
                "colWidth": 0.9,
                "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                "rowHeight": 0.8, "showValue": "auto",
                "tooltip": {"mode": "single", "sort": "none"},
            }
        elif kind == "state-timeline":
            result["options"] = {
                "alignValue": "left",
                "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                "mergeValues": True, "rowHeight": 0.8, "showValue": "always",
                "tooltip": {"mode": "single", "sort": "none"},
            }
        return result

    class Layout:
        def __init__(self):
            self.y = 0
            self.target = panels
            self.collapsed = False
            self.child_y = 0

        def row(self, pid, title, *, collapsed=False):
            row_panel = {
                "id": pid, "title": title, "type": "row", "collapsed": collapsed,
                "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": [],
            }
            panels.append(row_panel)
            self.y += 1
            self.collapsed = collapsed
            self.target = row_panel["panels"] if collapsed else panels
            self.child_y = self.y

        def line(self, *placements):
            line_y = self.child_y if self.collapsed else self.y
            height = max(item[3] for item in placements)
            for item, x, width, item_height in placements:
                item["gridPos"] = {"x": x, "y": line_y, "w": width, "h": item_height}
                self.target.append(item)
            if self.collapsed:
                self.child_y += height
            else:
                self.y += height

    def m(name, by="variant", filters=S):
        if "$phase" in filters:
            return f'sum by ({by}) (max by ({by},phase) (recsys_workflow_{name}{{{filters}}}))'
        return f'max by ({by}) (recsys_workflow_{name}{{{filters}}})'

    def snapshot(name, by="variant", filters=S):
        """Return the last gauge value inside the selected dashboard range.

        The router exports the active experiment only. Prometheus retains older
        samples after their series become stale, so historical dashboard links
        must use a range lookup instead of an instant selector.
        """
        series = f'last_over_time(recsys_workflow_{name}{{{filters}}}[$__range])'
        if "$phase" in filters:
            return f'sum by ({by}) (max by ({by},phase) ({series}))'
        return f'max by ({by}) ({series})'

    def latest_label(name, filters=E):
        # Phase and verdict changes create distinct label sets. Select the one
        # with the newest source timestamp within the chosen time range.
        return (
            'topk(1, last_over_time(timestamp('
            f'recsys_workflow_{name}{{{filters}}})[$__range:]))'
        )

    def latest_series(name, group, identity, filters=E):
        """Keep the value from only the newest label-set for each identity.

        State changes such as RUNNING -> SUCCEEDED create a new Prometheus
        series because ``status`` is a label.  A plain ``last_over_time``
        therefore returns both states for historical ranges.  The timestamp
        side selects the newest series while ``and`` preserves its value.
        """
        values = (
            f'max by ({group}) (last_over_time('
            f'recsys_workflow_{name}{{{filters}}}[$__range]))'
        )
        newest = (
            f'topk by ({identity}) (1, last_over_time(timestamp('
            f'recsys_workflow_{name}{{{filters}}})[$__range:]))'
        )
        return f'({values}) and on ({group}) ({newest})'

    def metric_rate(name, by="source,variant", filters=S):
        return f'rate(({m(name, by, filters)})[$__rate_interval:15s])'

    def log(event):
        return ('{namespace=~"kagent|ci"} | json | __error__="" | '
                f'event=~"{event}" | experiment_id=~"$experiment" | event_id!=""')

    layout = Layout()
    layout.line((panel(101, "", kind="text", text=(
        '<div style="font-size:22px;font-weight:700;line-height:1.2">LLM Agent A/B Rollout</div>'
        '<div style="color:#D1D5DB;margin-top:6px">'
        '<b>A / Control</b> = current model · <b>B / Candidate</b> = model under test</div>'
    )), 0, 24, 3))

    latest_ab_candidate_p95 = latest_series(
        "gate_window_p95_seconds",
        "sample_source,variant,phase,window_index,verdict",
        "sample_source,variant",
        E + ',phase="AB",variant="candidate",latest="true"',
    )
    latest_ab_control_p95 = latest_series(
        "gate_window_p95_seconds",
        "sample_source,variant,phase,window_index,verdict",
        "sample_source,variant",
        E + ',phase="AB",variant="control",latest="true"',
    )
    ab_ratio = (
        f'max by (sample_source) ({latest_ab_candidate_p95}) / '
        f'max by (sample_source) ({latest_ab_control_p95})'
    )
    layout.line(
        (panel(102, "Rollout status", {
            "{{phase}}": latest_label("info"),
        }, kind="stat", text_mode="name", nodata="UNKNOWN"), 0, 4, 4),
        (panel(103, "Decision", {
            "{{verdict}}": latest_label("gate"),
        }, kind="stat", text_mode="name", nodata="UNKNOWN"), 4, 4, 4),
        (panel(221, "Public A2A 20/20", {"completed / 20": (
            snapshot("external_case_count", "status", E + ',status="COMPLETED"') + ' / 20'
        )}, kind="stat", unit="percentunit", nodata="UNAVAILABLE",
               thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
               min_value=0, max_value=1,
               description="Exactly 20 one-shot HTTPS requests must complete through DNS, TLS, NGINX and the public A2A edge."), 8, 4, 4),
        (panel(155, "Evaluation confirmed", {"confirmed": (
            snapshot("evaluation_confirmed", "experiment_id", E) + ' / ' +
            snapshot("evaluation_expected", "experiment_id", E)
        )}, kind="stat", unit="percentunit",
               thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
               min_value=0, max_value=1), 12, 4, 4),
        (panel(130, "p95 B / A · ≤1.2", {"{{sample_source}}": ab_ratio}, kind="stat",
               thresholds=[{"color": "green", "value": None},
                           {"color": "red", "value": 1.200000001}],
               min_value=0, max_value=1.5,
               description="<1: candidate is faster; 1: equal; >1: candidate is slower. PASS requires ≤1.2."), 16, 8, 4),
    )

    layout.row(223, "Rollout controller · one action per Jenkins build")
    layout.line(
        (panel(224, "Controller decision", {
            "{{decision}}": latest_label("controller_last_tick"),
        }, kind="stat", text_mode="name", nodata="UNKNOWN / STALE"), 0, 4, 5),
        (panel(225, "Traffic Job", {
            "{{status}} · {{traffic_phase}}": latest_label("traffic_job_info"),
        }, kind="stat", text_mode="name", nodata="NOT STARTED"), 4, 4, 5),
        (panel(226, "Traffic heartbeat age", {
            "seconds": "time() - " + snapshot("traffic_job_heartbeat", "experiment_id", E),
        }, kind="stat", unit="s", nodata="UNKNOWN / STALE",
               thresholds=[{"color": "green", "value": None},
                           {"color": "yellow", "value": 60},
                           {"color": "red", "value": 120}]), 8, 4, 5),
        (panel(227, "Jenkins actions", {
            "{{action}} → {{target_weight}}% · {{status}}": latest_series(
                "controller_build",
                "action,target_weight,status",
                "action,target_weight",
                E,
            ),
        }, kind="bargauge", nodata="NOT DISPATCHED",
               description="Each value is a separate short Jenkins build number; Jenkins never selects the next action."), 12, 6, 5),
        (panel(228, "Traffic submitted", {
            "{{traffic_kind}}": snapshot("traffic_submitted", "traffic_kind", E),
        }, kind="bargauge", nodata="NO TRAFFIC",
               description="live_test is capped at 360; synthetic_case is exactly 20 with no retry or top-up."), 18, 6, 5),
    )

    layout.row(113, "Traffic rollout · live_test only")
    operational_filter = E + ',source="live_test",phase=~"$phase"'
    completed = m("completed_total", "variant", operational_filter)
    candidate_completed = m(
        "completed_total", "variant", operational_filter + ',variant="candidate"'
    )
    actual_share = f'100 * ({candidate_completed}) / sum({completed})'
    layout.line(
        (panel(114, "Operational throughput · root requests", {
            "{{variant}}": metric_rate("completed_total", "variant", operational_filter),
        }, unit="reqps", nodata="NO TRAFFIC"), 0, 12, 8),
        (panel(119, "Candidate traffic · target vs Envoy vs observed", {
            "Target": m("intended_weight", "experiment_id", E),
            "Envoy verified": m("verified_weight", "experiment_id", E),
            "Observed live_test": actual_share,
        }, unit="percent", min_value=0, max_value=100,
               description="Target is the requested pipeline weight; Envoy is the verified weight; observed is the actual live_test root share."), 12, 12, 8),
    )

    layout.row(120, "Online evaluation · synthetic responses")
    passed_by_branch = ('sum by (variant) (max by (case_id,variant) '
                        f'(last_over_time(recsys_workflow_case{{{E},verdict="PASS"}}[$__range])))')
    layout.line(
        (panel(123, "Total passing responses · by variant", {"{{variant}}": passed_by_branch},
               kind="piechart", nodata="NO PASSING RESPONSES",
               description="PASS responses only; the configured total is shown by Evaluation confirmed."), 0, 12, 7),
        (panel(222, "Public edge tickets & transport errors", {
            "{{status}}": snapshot(
                "external_case_count", "status",
                E + ',status=~"COMPLETED|AMBIGUOUS|REJECTED"',
            ),
            "error · {{error_type}}": snapshot("external_case_errors", "error_type", E),
        }, kind="bargauge", nodata="UNAVAILABLE",
               description="ISSUED → DISPATCH_INTENT → CLAIMED → COMPLETED. AMBIGUOUS or REJECTED is never retried or topped up."), 12, 12, 7),
    )

    layout.row(125, "Performance & reliability · source selector applies")
    layout.line(
        (panel(126, "Latency p50 · root workflow", {
            "{{source}} · {{variant}}":
                f'histogram_quantile(0.50, {metric_rate("duration_seconds_bucket", "source,variant,le")})',
        }, unit="s", nodata="NO TRAFFIC"), 0, 8, 7),
        (panel(127, "Latency p95 · root workflow", {
            "{{source}} · {{variant}}":
                f'histogram_quantile(0.95, {metric_rate("duration_seconds_bucket", "source,variant,le")})',
        }, unit="s", nodata="NO TRAFFIC",
               description="Rolling exploration histogram; the top ratio card uses the closed AB window."), 8, 8, 7),
        (panel(128, "Latency p99 · root workflow", {
            "{{source}} · {{variant}}":
                f'histogram_quantile(0.99, {metric_rate("duration_seconds_bucket", "source,variant,le")})',
        }, unit="s", nodata="NO TRAFFIC"), 16, 8, 7),
    )
    completed_rate = metric_rate("completed_total")
    layout.line(
        (panel(131, "Error rate · timeout / runtime-A2A-MCP / contract", {
            "{{source}} · {{variant}} runtime/A2A/MCP":
                f'100 * {metric_rate("errors_total")} / {completed_rate}',
            "{{source}} · {{variant}} timeout":
                f'100 * {metric_rate("timeouts_total")} / {completed_rate}',
            "{{source}} · {{variant}} contract":
                f'100 * {metric_rate("contracts_total")} / {completed_rate}',
        }, unit="percent", nodata="NO TRAFFIC",
               description="Runtime, A2A and MCP failures are one combined upstream-error family in durable evidence; the chart does not invent a finer split."), 0, 24, 7),
    )

    layout.row(133, "Functional checks · deterministic, no LLM judge")
    functional_metrics = (
        "trajectory_match",
        "tool_arguments_match",
        "duplicate_tool_calls",
        "missing_user_safe",
        "schema_valid",
        "ranking_preserved",
        "empty_result_correct",
        "release_consistency",
        "functional_success",
    )

    def functional_pass_rate(variant, metric):
        labels = E + f',variant="{variant}",metric="{metric}"'
        passed = (
            'sum(max by (metric,score_status) '
            f'(last_over_time(recsys_workflow_quality_count{{{labels},score_status="PASS"}}[$__range])))'
        )
        applicable = (
            'sum(max by (metric,score_status) '
            f'(last_over_time(recsys_workflow_quality_count{{{labels},score_status=~"PASS|FAIL|UNKNOWN"}}[$__range])))'
        )
        return f'(100 * ({passed}) / ({applicable})) or on() vector(-1)'

    na_mapping = [{"type": "value", "options": {
        "-1": {"text": "N/A — 0 applicable", "color": "gray"}
    }}]

    layout.line(
        (panel(157, "Control · PASS rate on applicable cases", {
            metric: functional_pass_rate("control", metric)
            for metric in functional_metrics
        }, kind="bargauge", unit="percent", min_value=0, max_value=100, color="blue",
               mappings=na_mapping,
               description="Raw deterministic metrics. 100% means every applicable case passed; N/A means this branch received zero applicable cases."), 0, 12, 10),
        (panel(134, "Candidate · PASS rate on applicable cases", {
            metric: functional_pass_rate("candidate", metric)
            for metric in functional_metrics
        }, kind="bargauge", unit="percent", min_value=0, max_value=100, color="orange",
               mappings=na_mapping,
               description="Raw deterministic metrics. Any required FAIL or UNKNOWN blocks promotion; hard failures are never averaged away."), 12, 12, 10),
    )

    layout.row(107, "Release & Global Config · details", collapsed=True)
    elapsed = ('(max by (experiment_id) (recsys_workflow_promoted_at{' + E + '}) - '
               'max by (experiment_id) (recsys_workflow_experiment_started_at{' + E + '})) or '
               '(time() - max by (experiment_id) (recsys_workflow_experiment_started_at{' + E + '}))')
    layout.line(
        (panel(108, "Control / candidate immutable releases", {
            "releases": snapshot("release_info", "variant,role,release_id,config_id,llm_version_id,quantization", E),
        }, kind="table"), 0, 16, 6),
        (panel(104, "Champion / previous pointers", {
            "pointers": snapshot("pointer", "pointer,release_id", E),
        }, kind="table"), 16, 8, 6),
    )
    layout.line(
        (panel(105, "Experiment duration", {"duration": elapsed}, kind="stat", unit="s"), 0, 6, 5),
        (panel(109, "Global generation config", {
            "{{variant}} · {{parameter}}": m("global_config", "variant,parameter", E),
        }, kind="table"), 6, 18, 5),
    )
    layout.line(
        (panel(110, "Effective config for each agent", {
            "{{variant}} · {{role}} · {{parameter}}": m("effective_config", "variant,role,parameter", E),
        }, kind="table"), 0, 12, 7),
        (panel(111, "Fixed agent overrides", {
            "{{variant}} · {{role}} · {{parameter}}": m("override", "variant,role,parameter", E),
        }, kind="table"), 12, 12, 7),
    )
    layout.line(
        (panel(112, "Effective config diff and release provenance", {
            "events": log("ab.release"),
        }, kind="table", datasource=LOKI), 0, 24, 8),
    )

    layout.row(145, "Infrastructure & Telemetry · details", collapsed=True)
    infra_namespace = 'namespace=~"kagent|llm-inference"'
    layout.line(
        (panel(146, "Telemetry freshness", {
            "seconds": 'time() - ' + m("telemetry_timestamp", "experiment_id", E),
        }, kind="stat", unit="s", nodata="UNKNOWN / STALE",
               thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 60}, {"color": "red", "value": 120}]), 0, 6, 5),
        (panel(148, "Receiver / Job / Jenkins state", {
            "dispatch {{status}}": m("dispatch_status", "status", E),
            "queue age {{status}}": m("dispatch_queue_age_seconds", "status", E),
        }, kind="bargauge"), 6, 6, 5),
        (panel(149, "Gateway / adapter / backend readiness", {
            "{{namespace}} · {{deployment}}":
                f'max by (namespace,deployment) (kube_deployment_status_replicas_available{{{infra_namespace},deployment=~"recsys-workflow.*|recsys-ab-.*|rec-ab-.*|rec-llm-.*|qwen.*"}})',
        }, kind="bargauge"), 12, 6, 5),
        (panel(152, "Pod restarts", {
            "{{pod}}": f'max by (pod) (kube_pod_container_status_restarts_total{{{infra_namespace},pod=~"recsys-workflow.*|recsys-ab-.*|rec-ab-.*|rec-llm-.*|qwen.*"}})',
        }, kind="bargauge"), 18, 6, 5),
    )
    layout.line(
        (panel(150, "Pod CPU", {
            "{{pod}}": f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{infra_namespace},pod=~"recsys-workflow.*|recsys-ab-.*|rec-ab-.*|rec-llm-.*|qwen.*",container!=""}}[$__rate_interval]))',
        }, unit="cores"), 0, 12, 7),
        (panel(151, "Pod memory", {
            "{{pod}}": f'sum by (pod) (container_memory_working_set_bytes{{{infra_namespace},pod=~"recsys-workflow.*|recsys-ab-.*|rec-ab-.*|rec-llm-.*|qwen.*",container!=""}})',
        }, unit="bytes"), 12, 12, 7),
    )
    layout.line(
        (panel(147, "Missing trace / usage coverage", {
            "{{source}} · {{variant}} unknown": m("unknown", "source,variant"),
            "{{source}} · {{variant}} usage known": m("usage_known_total", "source,variant"),
        }, kind="bargauge"), 0, 24, 6),
    )

    # Keep only the four useful diagnostics and show them without another click.
    detail_panels = {
        child["id"]: child
        for row_panel in panels
        for child in row_panel.get("panels", [])
    }
    panels[:] = [item for item in panels if item["id"] not in {107, 145}]
    layout.y = max(item["gridPos"]["y"] + item["gridPos"]["h"] for item in panels)
    layout.target = panels
    layout.collapsed = False
    layout.row(220, "Diagnostics", collapsed=False)
    layout.line(
        (detail_panels[108], 0, 16, 6),
        (detail_panels[104], 16, 8, 6),
    )
    layout.line(
        (detail_panels[150], 0, 12, 7),
        (detail_panels[151], 12, 12, 7),
    )

    variables = [{
        "name": "experiment", "label": "Experiment", "type": "query", "datasource": PROM,
        "query": {
            "query": "query_result(sort_desc(max_over_time(recsys_workflow_experiment_started_at[$__range])))",
            "refId": "experiment",
        },
        "regex": '/experiment_id="([^"]+)"/',
        "multi": False, "includeAll": False, "refresh": 1,
    }]
    for name, label, values in (
        ("source", "Source", "production,synthetic,live_test"),
        ("phase", "Phase", "IDLE,DEPLOY,OFFLINE,CANARY,AB,VERIFY,COMPLETED,ROLLED_BACK,ROLLBACK_FAILED"),
    ):
        default = "$__all"
        variables.append({
            "name": name, "label": label, "type": "custom", "query": values,
            "multi": False, "includeAll": True, "allValue": ".*",
            "current": {
                "text": "All",
                "value": default,
            },
        })

    return {
        "uid": "recsys-llm-ab", "title": "LLM Agent A/B Rollout",
        "description": ("Production control room for immutable LLM agent A/B releases; "
                        "post-promotion monitoring disabled; shadow Disabled by design."),
        "schemaVersion": 40, "version": 19, "editable": False,
        "tags": ["recsys", "workflow", "ab-testing", "langfuse"],
        "refresh": "30s", "time": {"from": "now-24h", "to": "now"},
        "timezone": "browser", "annotations": {"list": []},
        "templating": {"list": variables}, "panels": panels,
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
