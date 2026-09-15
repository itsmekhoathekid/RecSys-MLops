import json
from pathlib import Path
from jenkins.python.llm_agent_cd.dashboard import build

ROOT = Path(__file__).resolve().parents[3]


def all_panels(dashboard):
    for item in dashboard["panels"]:
        yield item
        yield from item.get("panels", [])


def test_provisioned_dashboard_is_reproducible_and_keeps_uid():
    d = json.loads((ROOT / "infra/helm/recsys-observability/dashboards/llm-ab-rollout.json").read_text())
    assert d == build()
    assert d["uid"] == "recsys-llm-ab"
    flat = list(all_panels(d))
    assert len({p["id"] for p in flat}) == len(flat)
    assert len([p for p in flat if p["type"] == "row"]) == 6
    assert len(flat) == 31
    assert {v["name"] for v in d["templating"]["list"]} == {"experiment", "source", "phase"}
    wire = json.dumps(d)
    assert "post-promotion monitoring disabled" in wire
    assert "Disabled by design" in wire


def test_control_room_is_compact_varied_and_defaults_to_live_data():
    d = build()
    flat = list(all_panels(d))
    types = {p["type"] for p in flat}
    assert {"stat", "bargauge", "piechart", "timeseries", "table"} <= types
    top = {p["id"]: p for p in flat}
    assert {102, 103, 155, 130, 221, 222} <= top.keys()
    assert {121, 202} & top.keys() == set()
    variables = {v["name"]: v for v in d["templating"]["list"]}
    assert "sort_desc" in variables["experiment"]["query"]["query"]
    assert "max_over_time" in variables["experiment"]["query"]["query"]
    assert variables["source"]["includeAll"] is True
    assert variables["source"]["current"] == {"text": "All", "value": "$__all"}
    assert variables["phase"]["includeAll"] is True
    assert variables["phase"]["current"] == {"text": "All", "value": "$__all"}
    assert d["time"] == {"from": "now-24h", "to": "now"}
    # Keep the default view compact even with Diagnostics visible.
    visible = [p for p in d["panels"] if not p.get("collapsed")]
    assert max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in visible) <= 70
    details = [p for p in d["panels"] if p["id"] == 220]
    assert len(details) == 1 and details[0]["collapsed"] is False
    assert details[0]["panels"] == []
    assert not {106, 118, 121, 122, 124, 136, 137, 138, 139, 140, 141, 156, 202} & {
        p["id"] for p in flat
    }
    passing = next(p for p in flat if p["id"] == 123)
    assert passing["title"] == "Total passing responses · by variant"
    assert passing["type"] == "piechart"
    assert passing["options"]["legend"]["values"] == ["value", "percent"]
    assert next(p for p in flat if p["id"] == 101)["transparent"] is True
    intro = next(p for p in flat if p["id"] == 101)["options"]["content"]
    assert "A / Control</b> = current model · <b>B / Candidate</b> = model under test" in intro
    assert "Read left to right" not in intro and "20-case" not in intro


def test_branch_and_verdict_colors_are_explicit():
    wire = json.dumps(build())
    for name, color in (("control", "blue"), ("candidate", "orange"),
                        ("PASS", "green"), ("HOLD", "yellow"), ("FAIL", "red")):
        assert name in wire and f'"fixedColor": "{color}"' in wire
    quality = [next(p for p in all_panels(build()) if p["id"] == pid)
               for pid in (157, 134)]
    assert all(status in json.dumps(quality) for status in ("PASS", "FAIL", "UNKNOWN"))


def test_gate_cards_use_threshold_coloring_and_rollback_is_red():
    flat = list(all_panels(build()))
    for panel_id in (130, 155, 221):
        card = next(p for p in flat if p["id"] == panel_id)
        assert card["fieldConfig"]["defaults"]["color"] == {"mode": "thresholds"}
    wire = json.dumps(build())
    assert ".*ROLLED_BACK.*" in wire


def test_gate_queries_do_not_use_exploration_source_or_phase():
    for p in all_panels(build()):
        for t in p.get("targets", []):
            if "recsys_workflow_gate" in t["expr"]:
                assert "$source" not in t["expr"] and "$phase" not in t["expr"]
            assert "or vector(0)" not in t["expr"]
            assert "rate(recsys_workflow_case" not in t["expr"]
            assert "rate(recsys_workflow_inflight" not in t["expr"]
        if "datasource" in p:
            assert p["datasource"]["uid"] in {"Prometheus", "Loki"}


def test_loki_queries_skip_parse_errors_and_infra_namespace_is_real():
    for p in all_panels(build()):
        for target in p.get("targets", []):
            if p.get("datasource") == {"type": "loki", "uid": "Loki"}:
                assert '| json | __error__="" |' in target["expr"]
                assert target["instant"] is False
    wire = json.dumps(build())
    assert 'namespace=~\\"kagent|llm-inference\\"' in wire
    assert 'namespace=~\\"kagent|llm\\"' not in wire
    assert not any(p["id"] in {106, 124} for p in all_panels(build()))


def test_p95_ratio_uses_one_closed_ab_window_and_source():
    ratio = next(p for p in all_panels(build()) if p["id"] == 130)
    expr = ratio["targets"][0]["expr"]
    assert expr.count("recsys_workflow_gate_window_p95_seconds") == 4
    assert expr.count('phase="AB"') == 4
    assert expr.count('latest="true"') == 4
    assert "max by (sample_source)" in expr
    assert expr.count("last_over_time") == 4
    assert expr.count("topk by (sample_source,variant)") == 2


def test_jenkins_actions_keep_only_the_newest_status_per_action():
    actions = next(p for p in all_panels(build()) if p["id"] == 227)
    expr = actions["targets"][0]["expr"]
    assert "topk by (action,target_weight)" in expr
    assert "timestamp(recsys_workflow_controller_build" in expr
    assert "and on (action,target_weight,status)" in expr


def test_instant_snapshot_panels_remain_visible_for_historical_experiments():
    flat = {p["id"]: p for p in all_panels(build())}
    for panel_id in (104, 108, 123, 134, 155, 157, 221, 222):
        assert "last_over_time" in json.dumps(flat[panel_id]["targets"])
    assert "last_over_time(timestamp(" in flat[102]["targets"][0]["expr"]
    assert "last_over_time(timestamp(" in flat[103]["targets"][0]["expr"]


def test_shared_snapshot_rates_deduplicate_before_rate():
    for p in all_panels(build()):
        for t in p.get("targets", []):
            expr = t["expr"]
            if "rate(" in expr and "recsys_workflow_" in expr:
                assert "rate((sum by" in expr and "max by" in expr
                assert "[$__rate_interval:15s]" in expr


def test_requested_llm_ab_metrics_have_focused_visuals():
    flat = list(all_panels(build()))
    wire = json.dumps(build())
    requested = {
        "trajectory_match", "tool_arguments_match", "duplicate_tool_calls",
        "missing_user_safe", "schema_valid", "ranking_preserved",
        "empty_result_correct", "release_consistency", "functional_success",
    }
    quality_wire = json.dumps([p for p in flat if p["id"] in {157, 134}])
    assert all(metric in quality_wire for metric in requested)
    assert "recsys_workflow_evaluation_confirmed" in wire
    assert "recsys_workflow_external_case_count" in wire
    assert "recsys_workflow_external_case_errors" in wire
    assert "N/A" in wire and "0 applicable" in wire
    assert all(f"histogram_quantile({q}" in wire for q in ("0.50", "0.95", "0.99"))
    for metric in ("errors_total", "timeouts_total", "completed_total",
                   "intended_weight", "verified_weight"):
        assert f"recsys_workflow_{metric}" in wire
    assert "1.2" in json.dumps(next(p for p in flat if p["id"] == 130))
    assert "Execution cost" not in wire


def test_operational_traffic_is_not_mixed_with_twenty_synthetic_cases():
    flat = list(all_panels(build()))
    assert not any(p["id"] in {118, 140} for p in flat)
    traffic = next(p for p in flat if p["id"] == 119)
    assert any('source="live_test"' in target["expr"] for target in traffic["targets"])

    quality = [next(p for p in flat if p["id"] == pid) for pid in (157, 134)]
    assert all(p["fieldConfig"]["defaults"]["unit"] == "percent" for p in quality)
    assert quality[0]["fieldConfig"]["defaults"]["color"]["fixedColor"] == "blue"
    assert quality[1]["fieldConfig"]["defaults"]["color"]["fixedColor"] == "orange"
    assert all(len(p["targets"]) == 9 for p in quality)
    quality_wire = json.dumps(quality)
    for metric in (
        "trajectory_match", "tool_arguments_match", "duplicate_tool_calls",
        "missing_user_safe", "schema_valid", "ranking_preserved",
        "empty_result_correct", "release_consistency", "functional_success",
    ):
        assert metric in quality_wire
