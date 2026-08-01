from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass

def query_prometheus(prometheus_url: str, query: str) -> float:
    encoded = urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(f"{prometheus_url.rstrip('/')}/api/v1/query?{encoded}", timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result = payload.get("data", {}).get("result", [])
    if not result:
        return 0.0
    return float(result[0]["value"][1])


@dataclass(frozen=True)
class GateDecision:
    decision: str
    reasons: list[str]
    metrics: dict[str, float]
    experiment_id: str
    gate_window: str


def evaluate_candidate_gates(
    prometheus_url: str,
    gate_window: str,
    *,
    experiment_id: str = "",
    max_error_delta: float = 0.02,
    max_latency_ratio: float = 1.5,
    min_quality_ratio: float = 0.95,
    min_samples: int = 100,
) -> GateDecision:
    if not prometheus_url:
        return GateDecision("hold", ["prometheus URL is required"], {}, experiment_id, gate_window)
    experiment = experiment_id.replace("\\", "\\\\").replace('"', '\\"')
    experiment_matcher = f',experiment_id="{experiment}"' if experiment else ""
    candidate_samples = query_prometheus(
        prometheus_url,
        f'sum(increase(model_predictions_total{{ab_variant="candidate"{experiment_matcher}}}[{gate_window}]))',
    )
    control_samples = query_prometheus(
        prometheus_url,
        f'sum(increase(model_predictions_total{{ab_variant="control"{experiment_matcher}}}[{gate_window}]))',
    )
    candidate_error = query_prometheus(
        prometheus_url,
        f'sum(rate(model_predictions_total{{ab_variant="candidate",status="error"{experiment_matcher}}}[{gate_window}])) '
        f'/ clamp_min(sum(rate(model_predictions_total{{ab_variant="candidate"{experiment_matcher}}}[{gate_window}])), 0.001)',
    )
    control_error = query_prometheus(
        prometheus_url,
        f'sum(rate(model_predictions_total{{ab_variant="control",status="error"{experiment_matcher}}}[{gate_window}])) '
        f'/ clamp_min(sum(rate(model_predictions_total{{ab_variant="control"{experiment_matcher}}}[{gate_window}])), 0.001)',
    )
    candidate_latency = query_prometheus(
        prometheus_url,
        "histogram_quantile(0.95, "
        f'sum(rate(model_prediction_latency_seconds_bucket{{ab_variant="candidate"{experiment_matcher}}}[{gate_window}])) by (le))',
    )
    control_latency = query_prometheus(
        prometheus_url,
        "histogram_quantile(0.95, "
        f'sum(rate(model_prediction_latency_seconds_bucket{{ab_variant="control"{experiment_matcher}}}[{gate_window}])) by (le))',
    )
    candidate_quality = query_prometheus(
        prometheus_url,
        f'sum(rate(model_prediction_confidence_sum{{ab_variant="candidate"{experiment_matcher}}}[{gate_window}])) '
        f'/ clamp_min(sum(rate(model_prediction_confidence_count{{ab_variant="candidate"{experiment_matcher}}}[{gate_window}])), 0.001)',
    )
    control_quality = query_prometheus(
        prometheus_url,
        f'sum(rate(model_prediction_confidence_sum{{ab_variant="control"{experiment_matcher}}}[{gate_window}])) '
        f'/ clamp_min(sum(rate(model_prediction_confidence_count{{ab_variant="control"{experiment_matcher}}}[{gate_window}])), 0.001)',
    )
    metrics = {
        "candidate_samples": candidate_samples,
        "control_samples": control_samples,
        "candidate_error_rate": candidate_error,
        "control_error_rate": control_error,
        "candidate_p95_latency_seconds": candidate_latency,
        "control_p95_latency_seconds": control_latency,
        "candidate_quality_proxy": candidate_quality,
        "control_quality_proxy": control_quality,
    }
    if candidate_samples < min_samples or control_samples < min_samples:
        return GateDecision(
            "hold",
            [f"insufficient samples: candidate={candidate_samples}, control={control_samples}, minimum={min_samples}"],
            metrics,
            experiment_id,
            gate_window,
        )
    reasons = []
    if candidate_error > control_error + max_error_delta:
        reasons.append(f"candidate error gate failed: candidate={candidate_error}, control={control_error}")
    if control_latency > 0 and candidate_latency > control_latency * max_latency_ratio:
        reasons.append(f"candidate latency gate failed: candidate={candidate_latency}, control={control_latency}")
    if control_quality > 0 and candidate_quality < control_quality * min_quality_ratio:
        reasons.append(f"candidate quality proxy gate failed: candidate={candidate_quality}, control={control_quality}")
    return GateDecision("rollback" if reasons else "promote", reasons, metrics, experiment_id, gate_window)


def assert_promote_gates(
    prometheus_url: str,
    gate_window: str,
    experiment_id: str = "",
    *,
    max_error_delta: float = 0.02,
    max_latency_ratio: float = 1.5,
    min_quality_ratio: float = 0.95,
    min_samples: int = 0,
) -> None:
    if not prometheus_url:
        return
    decision = evaluate_candidate_gates(
        prometheus_url,
        gate_window,
        experiment_id=experiment_id,
        max_error_delta=max_error_delta,
        max_latency_ratio=max_latency_ratio,
        min_quality_ratio=min_quality_ratio,
        min_samples=min_samples,
    )
    if decision.decision != "promote":
        raise RuntimeError(decision.reasons[0])
