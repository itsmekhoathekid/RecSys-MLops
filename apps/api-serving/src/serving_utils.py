from __future__ import annotations

import os


def bool_env(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


def int_env(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


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
