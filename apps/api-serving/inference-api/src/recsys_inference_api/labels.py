from __future__ import annotations


def ab_labels(
    ab_variant: str | None,
    model_version: str,
    ab_experiment_id: str | None,
) -> dict[str, str]:
    return {
        "ab_variant": ab_variant or "none",
        "model_version": model_version,
        "experiment_id": ab_experiment_id or "none",
    }
