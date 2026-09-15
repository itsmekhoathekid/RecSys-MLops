from __future__ import annotations

import math


def _fault_reason(arm: str, stats: dict, *, organic: bool = False) -> str | None:
    prefix = f"organic {arm}" if organic else arm
    errors = stats.get("errors", 0)
    contract_failures = stats.get("contract_failures", 0)
    has_errors = type(errors) in (int, float) and math.isfinite(errors) and errors > 0
    has_contract_failures = (
        type(contract_failures) in (int, float)
        and math.isfinite(contract_failures)
        and contract_failures > 0
    )
    if has_errors and has_contract_failures:
        return f"{prefix} runtime error and tool contract violation"
    if has_contract_failures:
        return f"{prefix} tool contract violation"
    if has_errors:
        return f"{prefix} runtime error"
    return None


def production_gate(
    observation: dict, policy: dict, *, compare: bool, champion_required: bool = True
):
    # Organic faults block acceptance even when bounded live-test samples supply
    # the statistical window. They must not disappear behind a dashboard filter.
    organic = observation.get("organic", {})
    for arm in ("champion", "candidate"):
        stats = organic.get(arm, {})
        if reason := _fault_reason(arm, stats, organic=True):
            return "FAIL", reason
    # Known failures take precedence over missing samples/telemetry on either
    # arm. A low-volume candidate must never hide an observed control fault.
    for arm in ("champion", "candidate"):
        stats = observation.get(arm, {})
        if reason := _fault_reason(arm, stats):
            return "FAIL", reason
    if observation.get("healthy") is not True:
        return "HOLD", "telemetry unavailable or stale"
    arms = ["candidate", "champion"] if champion_required else ["candidate"]
    for arm in arms:
        stats = observation.get(arm, {})
        for field in ("count", "errors", "contract_failures", "unknown"):
            v = stats.get(field)
            if not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                return "HOLD", f"missing/invalid {arm}.{field}"
        if stats["errors"] or stats["contract_failures"]:
            return "FAIL", _fault_reason(arm, stats)
        if stats["unknown"]:
            return "HOLD", f"{arm} missing contract evidence"
        if stats["count"] < policy["min_samples"]:
            return "HOLD", f"{arm} insufficient production samples"
    if compare:
        a, b = observation["champion"].get("p95"), observation["candidate"].get("p95")
        if any(
            not isinstance(v, (float, int)) or not math.isfinite(v) or v <= 0
            for v in (a, b)
        ):
            return "HOLD", "missing/invalid latency"
        if b > a * policy["latency_ratio"]:
            return "FAIL", "candidate latency regression"
    return "PASS", "production gates satisfied"


def case_gate(cases: dict, champion: str, candidate: str, evaluation_version=None):
    if len(cases) != 20:
        return "HOLD", "exactly 20 cases required"
    if any(row.get("verdict") == "FAIL" for row in cases.values()):
        return "FAIL", "synthetic assertion failed"
    if evaluation_version:
        evaluations = [r.get("evaluation", {}) for r in cases.values()]
        if any(e.get("verdict") == "FAIL" for e in evaluations):
            return "FAIL", "code evaluation hard gate failed"
        if any(e.get("evaluator_version") != evaluation_version or e.get("verdict") != "PASS"
               or e.get("synced") is not True for e in evaluations):
            return "HOLD", "code evaluation or Langfuse score confirmation pending"
    if any(row.get("verdict") != "PASS" for row in cases.values()):
        return "HOLD", "synthetic outcome missing or ambiguous; never replay"
    counts = {
        arm: sum(row.get("release_id") == arm for row in cases.values())
        for arm in (champion, candidate)
    }
    if sum(counts.values()) != 20 or min(counts.values()) < 5:
        return "HOLD", "insufficient per-arm synthetic samples; no top-up"
    return "PASS", "20 functional cases passed (not statistical superiority)"
