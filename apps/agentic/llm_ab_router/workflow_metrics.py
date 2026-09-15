"""Replica-independent metrics from durable workflow records.

Each router exports the SAME DB snapshot. Dashboard queries deduplicate exporter
replicas with max by (...), never sum replicas. Only immutable completed events
are counters/histograms; mutable progress and in-flight observations are gauges.
"""
import math
from .database import json_label
from jenkins.python.llm_agent_cd.workflow import members

BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600)


def render(db, state):
    eid = state.get("experiment_id", "baseline")
    scope = state.get("champion", {}).get("scope", "recommendation")
    base = {"experiment_id": eid, "scope": scope}
    lines = []
    types = set()
    def metric(name, value, labels=None, kind="gauge"):
        if value is None:
            return
        try:
            if not math.isfinite(float(value)):
                return
        except (TypeError, ValueError):
            return
        if name not in types:
            lines.append("# TYPE " + name + " " + kind)
            types.add(name)
        lab = {**base, **(labels or {})}
        wire = ",".join(k + "=" + json_label(v) for k, v in sorted(lab.items()))
        lines.append(name + "{" + wire + "} " + str(value))

    phase = state["phase"]
    info = {"phase": phase, "mode": state.get("mode", "none"),
            "sample_source": state.get("policy", {}).get("sample_source", "production"),
            "monitor": "disabled" if not state.get("policy", {}).get("monitor_seconds", 0) else "legacy"}
    metric("recsys_workflow_info", 1, info)
    metric("recsys_workflow_gate", 1, {**info, "verdict": state.get("gate", {}).get("verdict", "HOLD")})
    metric("recsys_workflow_intended_weight", state.get("route_intent", {}).get("weight", 0))
    metric("recsys_workflow_verified_weight", state.get("verified_weight"))
    metric("recsys_workflow_started_at", state.get("stage_started"))
    metric("recsys_workflow_experiment_started_at", state.get("experiment_started"))
    metric("recsys_workflow_promoted_at", state.get("promoted_at"))
    arms = {}
    for variant, key in (("control", "baseline"), ("candidate", "pending")):
        pointer = state.get(key)
        if not pointer:
            continue
        r = state.get("releases", {}).get(pointer.get("release_id"), pointer)
        # Baseline==pending during bootstrap is control-only, not two copies.
        if r["release_id"] in arms:
            continue
        arms[r["release_id"]] = variant
        generation = r.get("global_generation", r.get("config", {}))
        for parameter, value in generation.items():
            metric("recsys_workflow_global_config", value, {"variant": variant, "parameter": parameter})
        variants = members(r) if scope == "workflow" else {"recommendation": r}
        for role, m in variants.items():
            # State recovery can expose an ID-only pointer before its immutable
            # manifest is loaded. Keep /metrics available and omit only the
            # release-info sample whose labels are not yet knowable.
            if not all(key in m for key in ("config_id", "llm_version_id", "llm")):
                continue
            labels = {"variant": variant, "role": role, "release_id": r["release_id"],
                      "config_id": m["config_id"], "llm_version_id": m["llm_version_id"],
                      "quantization": m["llm"]["quantization"]}
            metric("recsys_workflow_release_info", 1, labels)
            for parameter, value in m["config"].items():
                metric("recsys_workflow_effective_config", value, {"variant": variant, "role": role, "parameter": parameter})
                overridden = parameter in r.get("agent_overrides", {}).get(role, {})
                metric("recsys_workflow_override", int(overridden), {"variant": variant, "role": role, "parameter": parameter})
    for pointer in ("champion", "previous"):
        if state.get(pointer):
            metric("recsys_workflow_pointer", 1, {"pointer": pointer, "release_id": state[pointer]["release_id"]})
    for f in state.get("fixtures", []):
        row = state.get("cases", {}).get(f["id"], {})
        metric("recsys_workflow_case", 1, {"case_id": f["id"], "group": f.get("group", f.get("category", "unknown")),
            "verdict": row.get("verdict", "NOT_STARTED"), "variant": arms.get(row.get("release_id"), "unassigned")})
        metric("recsys_workflow_case_duration_seconds", row.get("duration_seconds"), {"case_id": f["id"]})
    metric('recsys_workflow_evaluation_expected', len(state.get('fixtures', [])))
    for row in state.get('offline_evidence',{}).get('cases',[]):
        metric('recsys_workflow_offline_case',1,{'variant':row['variant'],'case_id':row['case_id'],
            'verdict':(row['result'] or {}).get('verdict','HOLD'),'synced':str(row['synced']).lower()})
    gate = state.get("gate_evidence", {})
    for key in ("start", "end", "latency_ratio"):
        metric("recsys_workflow_gate_" + key, gate.get(key))
    for arm, observations in gate.get("observation", {}).items():
        if not isinstance(observations, dict):
            continue
        variant = "control" if arm == "champion" else "candidate"
        for stat in ("count", "p95", "errors", "contract_failures", "unknown"):
            metric("recsys_workflow_gate_" + stat, observations.get(stat), {"variant": variant,
                   "sample_source": gate.get("source", "production"), "phase": gate.get("phase", phase)})

    # Keep every closed promotion window queryable after the pipeline advances.
    # The legacy recsys_workflow_gate_* series above intentionally remains the
    # current snapshot for backwards compatibility.  Closed windows use a
    # bounded, per-experiment index; arbitrary reasons and request identifiers
    # stay in Loki/MinIO instead of becoming Prometheus labels.
    windows = list(state.get("gate_windows", []))
    if gate and gate not in windows:
        windows.append(gate)
    latest_by_stage = {}
    for index, window in enumerate(windows):
        stage_key = (window.get("phase", "UNKNOWN"),
                     window.get("source", "production"))
        latest_by_stage[stage_key] = index
    for index, window in enumerate(windows):
        window_phase = window.get("phase", "UNKNOWN")
        window_source = window.get("source", "production")
        common = {
            "phase": window_phase,
            "sample_source": window_source,
            "verdict": window.get("verdict", "HOLD"),
            "window_index": str(index),
            "latest": str(latest_by_stage[(window_phase, window_source)] == index).lower(),
        }
        metric("recsys_workflow_gate_window_info", 1, common)
        if window.get("start") is not None and window.get("end") is not None:
            metric("recsys_workflow_gate_window_duration_seconds",
                   window["end"] - window["start"], common)
        metric("recsys_workflow_gate_window_latency_ratio_limit",
               window.get("latency_ratio"), common)
        for arm, observations in window.get("observation", {}).items():
            if not isinstance(observations, dict):
                continue
            variant = "control" if arm == "champion" else "candidate"
            labels = {**common, "variant": variant}
            for source_key, metric_suffix in (
                ("count", "count"),
                ("p95", "p95_seconds"),
                ("errors", "errors"),
                ("contract_failures", "contract_failures"),
                ("unknown", "unknown"),
            ):
                metric("recsys_workflow_gate_window_" + metric_suffix,
                       observations.get(source_key), labels)
    with db.connect() as c:
        extra = ",".join(f"count(*) FILTER(WHERE finished_at IS NOT NULL AND extract(epoch FROM finished_at-started_at)<={b}) AS b{i}" for i, b in enumerate(BUCKETS))
        rows = c.execute("""SELECT release_id,source,coalesce(result->>'phase','UNKNOWN') AS phase,
            count(*) FILTER(WHERE finished_at IS NOT NULL) AS completed,
            count(*) FILTER(WHERE finished_at IS NULL) AS inflight,
            count(*) FILTER(WHERE result->>'error'='true') AS errors,
            count(*) FILTER(WHERE result->>'timeout'='true') AS timeouts,
            count(*) FILTER(WHERE result->>'contract_failure'='true') AS contracts,
            count(*) FILTER(WHERE result->>'verdict'='HOLD') AS unknown,
            count(DISTINCT session_key) FILTER (WHERE session_key IN
              (SELECT session_key FROM recsys_ab.sessions WHERE created_at >= to_timestamp(%s))
              AND started_at = (SELECT min(first.started_at) FROM recsys_ab.invocations first
                                WHERE first.session_key=recsys_ab.invocations.session_key)) AS sessions,
            sum(extract(epoch FROM finished_at-started_at)) AS duration,
            sum((result->>'input_tokens')::bigint) AS input_tokens,
            sum((result->>'output_tokens')::bigint) AS output_tokens,
            count(*) FILTER(WHERE result ? 'input_tokens' AND result ? 'output_tokens') AS usage_known,
            sum((result->>'child_calls')::bigint) AS child_calls,
            sum((result->>'tool_calls')::bigint) AS tool_calls,
            """ + extra + " FROM recsys_ab.invocations WHERE experiment_id=%s GROUP BY release_id,source,coalesce(result->>'phase','UNKNOWN')", (state.get("experiment_started", state.get("stage_started", 0)), eid)).fetchall()
        heartbeat_prefix = "recsys-workflow-router%" if scope == "workflow" else "recsys-ab-router%"
        heartbeat = c.execute("SELECT extract(epoch FROM max(seen_at)) AS at FROM recsys_ab.heartbeat WHERE instance LIKE %s", (heartbeat_prefix,)).fetchone()["at"]
        agent_rows = c.execute("""SELECT release_id,source,a->>'role' AS role,coalesce(result->>'phase','UNKNOWN') AS phase,
            count(*) AS completed, count(*) FILTER (WHERE a->>'error'='true') AS errors,
            sum((a->>'input_tokens')::bigint) AS input_tokens,
            sum((a->>'output_tokens')::bigint) AS output_tokens
            FROM recsys_ab.invocations CROSS JOIN LATERAL jsonb_array_elements(result->'agents') a
            WHERE experiment_id=%s AND finished_at IS NOT NULL GROUP BY release_id,source,a->>'role',coalesce(result->>'phase','UNKNOWN')""", (eid,)).fetchall()
        evaluation_health = c.execute('''SELECT count(*) FILTER(WHERE confirmed_at IS NULL) AS pending,
            count(*) FILTER(WHERE confirmed_at IS NULL AND attempts>0) AS sync_pending,
            count(*) FILTER(WHERE confirmed_at IS NOT NULL) AS confirmed,
            count(*) FILTER(WHERE evaluation->>'verdict' IN ('PASS','FAIL')) AS covered
            FROM recsys_ab.evaluation_outbox WHERE experiment_id=%s AND metadata->>'source'='synthetic' ''',(eid,)).fetchone()
        quality_rows = c.execute('''SELECT metadata->>'variant' AS variant,
            score->>'name' AS metric, score->>'status' AS score_status, count(*) AS count
            FROM recsys_ab.evaluation_outbox
            CROSS JOIN LATERAL jsonb_array_elements(evaluation->'scores') score
            WHERE experiment_id=%s AND metadata->>'source'='synthetic'
              AND confirmed_at IS NOT NULL
            GROUP BY metadata->>'variant',score->>'name',score->>'status' ''',(eid,)).fetchall()
        evaluator_rows = c.execute('''SELECT DISTINCT evaluation->>'evaluator_version' AS version
            FROM recsys_ab.evaluation_outbox WHERE experiment_id=%s
              AND metadata->>'source'='synthetic' AND confirmed_at IS NOT NULL
              AND evaluation->>'evaluator_version' IS NOT NULL''',(eid,)).fetchall()
        ticket_run = c.execute('''SELECT status FROM recsys_ab.external_case_runs
            WHERE experiment_id=%s''',(eid,)).fetchone()
        ticket_rows = c.execute('''SELECT status,count(*) AS count
            FROM recsys_ab.external_case_tickets WHERE experiment_id=%s GROUP BY status''',(eid,)).fetchall()
        ticket_error_rows = c.execute('''SELECT last_error,count(*) AS count
            FROM recsys_ab.external_case_tickets WHERE experiment_id=%s
              AND last_error IS NOT NULL GROUP BY last_error''',(eid,)).fetchall()
        dispatch_rows = c.execute("""SELECT experiment_id,status,
            extract(epoch FROM created_at) AS created,
            extract(epoch FROM updated_at) AS updated,
            CASE WHEN status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED')
              THEN greatest(0,extract(epoch FROM now()-created_at)) END AS queue_age
            FROM recsys_ab.trigger_requests WHERE experiment_id LIKE %s ORDER BY created_at""",
            ("wf-%" if scope == "workflow" else "rec-%",)).fetchall()
        controller_rows = c.execute("""SELECT action,target_weight,status,build_number,
            extract(epoch FROM updated_at) AS updated FROM recsys_ab.controller_actions
            WHERE experiment_id=%s ORDER BY created_at""", (eid,)).fetchall()
        controller_status = c.execute("""SELECT decision,extract(epoch FROM last_tick_at) AS last_tick
            FROM recsys_ab.controller_status WHERE experiment_id=%s""", (eid,)).fetchone()
        traffic_run = c.execute("""SELECT status,phase,live_submitted,synthetic_submitted,
            extract(epoch FROM heartbeat_at) AS heartbeat FROM recsys_ab.traffic_runs
            WHERE experiment_id=%s""", (eid,)).fetchone()
        traffic_ticket_rows = c.execute("""SELECT status,count(*) AS count
            FROM recsys_ab.live_traffic_tickets WHERE experiment_id=%s GROUP BY status""",
            (eid,)).fetchall()
    metric('recsys_workflow_evaluation_pending', evaluation_health.get('pending'))
    metric('recsys_workflow_evaluation_sync_pending', evaluation_health.get('sync_pending'))
    metric('recsys_workflow_evaluation_confirmed', evaluation_health.get('confirmed'))
    metric('recsys_workflow_evaluation_covered', evaluation_health.get('covered'))
    for row in quality_rows:
        metric('recsys_workflow_quality_count', row['count'], {
            'variant': row['variant'], 'metric': row['metric'],
            'score_status': row['score_status']})
    for row in evaluator_rows:
        metric('recsys_workflow_evaluator_info', 1, {'evaluator_version': row['version']})
    if ticket_run and ticket_run.get('status'):
        metric('recsys_workflow_external_suite_info', 1, {
            'entrypoint': 'public_a2a', 'status': ticket_run['status']})
    for row in ticket_rows:
        metric('recsys_workflow_external_case_count', row['count'], {'status': row['status']})
    error_families = {
        'ConnectError': 'tls_or_connect', 'ConnectTimeout': 'timeout',
        'ReadTimeout': 'timeout', 'WriteTimeout': 'timeout',
        'PoolTimeout': 'timeout', 'HTTPStatusError': 'http',
        'ProtocolError': 'protocol', 'JSONDecodeError': 'protocol',
        'RuntimeError': 'edge_or_upstream', 'ValueError': 'invalid_request',
    }
    grouped_errors = {}
    for row in ticket_error_rows:
        family = error_families.get(row['last_error'], 'other')
        grouped_errors[family] = grouped_errors.get(family, 0) + row['count']
    for family, count in grouped_errors.items():
        metric('recsys_workflow_external_case_errors', count, {'error_type': family})
    # These are shared DB gauges. They moved from the removed Recommendation
    # receiver Deployment to the already-scraped router /metrics endpoint.
    for row in dispatch_rows:
        labels = {"experiment_id": row["experiment_id"], "status": row["status"]}
        metric("recsys_workflow_dispatch_status", 1, labels)
        metric("recsys_workflow_dispatch_updated_at", row["updated"], labels)
        metric("recsys_workflow_dispatch_queue_age_seconds", row["queue_age"], labels)
    for row in controller_rows:
        labels = {"action": row["action"], "status": row["status"],
                  "target_weight": row["target_weight"] if row["target_weight"] is not None else "none"}
        metric("recsys_workflow_controller_action_info", 1, labels)
        metric("recsys_workflow_controller_action_updated_at", row["updated"], labels)
        metric("recsys_workflow_controller_build", row["build_number"], labels)
    if controller_status and "last_tick" in controller_status:
        metric("recsys_workflow_controller_last_tick", controller_status["last_tick"],
               {"decision": controller_status["decision"]})
    if traffic_run and "status" in traffic_run:
        labels = {"status": traffic_run["status"], "traffic_phase": traffic_run["phase"] or "WAITING"}
        metric("recsys_workflow_traffic_job_info", 1, labels)
        metric("recsys_workflow_traffic_job_heartbeat", traffic_run["heartbeat"])
        metric("recsys_workflow_traffic_submitted", traffic_run["live_submitted"],
               {"traffic_kind": "live_test"})
        metric("recsys_workflow_traffic_submitted", traffic_run["synthetic_submitted"],
               {"traffic_kind": "synthetic_case"})
    for row in traffic_ticket_rows:
        metric("recsys_workflow_live_ticket_count", row["count"], {"status": row["status"]})
    metric("recsys_workflow_telemetry_timestamp", heartbeat)
    for row in rows:
        if row["release_id"] not in arms:
            continue
        labels = {"variant": arms[row["release_id"]], "source": row["source"], "phase": row["phase"], "role": "root"}
        for key in ("completed", "errors", "timeouts", "contracts", "sessions", "input_tokens", "output_tokens", "usage_known", "child_calls", "tool_calls"):
            metric("recsys_workflow_" + key + "_total", row[key], labels, "counter")
        for key in ("inflight", "unknown"):
            metric("recsys_workflow_" + key, row[key], labels)
        # Bucket samples are completed-event counters; rates use a subquery after HA deduplication.
        for i, bound in enumerate(BUCKETS):
            metric("recsys_workflow_duration_seconds_bucket", row["b" + str(i)], {**labels, "le": str(bound)}, "counter")
        metric("recsys_workflow_duration_seconds_bucket", row["completed"], {**labels, "le": "+Inf"}, "counter")
        metric("recsys_workflow_duration_seconds_count", row["completed"], labels, "counter")
        metric("recsys_workflow_duration_seconds_sum", row["duration"], labels, "counter")
        if scope == "recommendation":
            for key in ("completed", "errors", "input_tokens", "output_tokens"):
                metric("recsys_workflow_agent_" + key + "_total", row[key],
                       {**labels, "role": "recommendation"}, "counter")
    for row in agent_rows:
        if row["release_id"] in arms:
            for key in ("completed", "errors", "input_tokens", "output_tokens"):
                metric("recsys_workflow_agent_" + key + "_total", row[key],
                       {"variant": arms[row["release_id"]], "source": row["source"], "role": row["role"], "phase": row["phase"]}, "counter")
    return "\n".join(lines) + "\n"
