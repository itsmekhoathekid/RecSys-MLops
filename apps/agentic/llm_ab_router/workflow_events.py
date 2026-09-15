"""Redacted, deterministic projections for Loki; MinIO remains authoritative.

Delivery is at-least-once across replicas/restarts. Tables deduplicate event_id;
these log records must never be summed to decide a gate or count invocations.
"""
from jenkins.python.llm_agent_cd.release import digest
from jenkins.python.llm_agent_cd.workflow import diff


def snapshot_events(state):
    eid = state.get("experiment_id", "baseline")
    scope = state.get("champion", {}).get("scope", "recommendation")
    common = {"experiment_id": eid, "scope": scope, "build_url": state.get("build_url", ""),
              "policy_checksum": state.get("policy_checksum", ""),
              "source": state.get("policy", {}).get("sample_source", "production")}
    out = []

    def add(event, key, data):
        out.append({**common, **data, "event": event, "event_id": digest([eid, event, key, data])})

    for i, event in enumerate(state.get("events", [])):
        # State events can contain full manifests. Do not emit bindings/headers,
        # credentials, user prompts, tool output, or arbitrary error strings.
        add("ab.phase", i, {k: event[k] for k in ("at", "phase", "route_revision", "verified_weight") if k in event})
    for gate in [*state.get("gate_windows", []), *([state["gate_evidence"]] if state.get("gate_evidence") else [])]:
        add("ab.gate", gate.get("end"), {**gate, "observation": gate.get("observation", {})})
    arms = {state[k]["release_id"]: arm for k, arm in (("baseline", "control"), ("pending", "candidate")) if state.get(k)}
    for f in state.get("fixtures", []):
        case = state.get("cases", {}).get(f["id"], {})
        data = {"case_id": f["id"], "group": f.get("group", "unknown"), "source": "synthetic",
                "phase": "AB", "variant": arms.get(case.get("release_id"), "unassigned"),
                **{k: case[k] for k in ("release_id", "verdict", "reason", "duration_seconds", "trace_id") if k in case}}
        data.setdefault("verdict", "NOT_STARTED")
        add("ab.case", f["id"], data)
        evaluation = case.get('evaluation_evidence', {})
        for score in evaluation.get('scores', []):
            add('ab.quality', [f['id'],score['name']], {**data,
                'metric':score['name'],'score_status':score['status'],'value':score['value'],
                'required':score['required'],'evaluator_version':evaluation.get('evaluator_version'),
                'evidence_checksum':evaluation.get('evidence_checksum'),
                'synced':case.get('evaluation',{}).get('synced',False)})
    for row in state.get('offline_evidence',{}).get('cases',[]):
        add('ab.offline',[row['variant'],row['case_id']],{'source':'offline',
            'variant':row['variant'],'case_id':row['case_id'],'release_id':row['release_id'],
            'verdict':(row['result'] or {}).get('verdict','HOLD'),'synced':row['synced'],
            'evaluator_version':state.get('offline_evidence',{}).get(
                'evaluator_version','compatibility-smoke-v1'),
            'fixture_checksum':row['fixture_checksum']})
    if state.get("baseline", {}).get("scope") == "workflow" and state.get("pending"):
        for role, data in diff(state["baseline"], state["pending"]).items():
            add("ab.release", role, {"role": role, **data})
    elif state.get("baseline") and state.get("pending"):
        before, after = state["baseline"], state["pending"]
        add("ab.release", "recommendation", {
            "role": "recommendation",
            "changed": before["release_id"] != after["release_id"],
            "config_changed": before["config_id"] != after["config_id"],
            "llm_changed": before["llm_version_id"] != after["llm_version_id"],
            "control_release_id": before["release_id"],
            "candidate_release_id": after["release_id"],
        })
    return out
