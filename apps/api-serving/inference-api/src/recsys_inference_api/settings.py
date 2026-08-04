from __future__ import annotations

import os
from dataclasses import dataclass

from recsys_serving_common.env import int_env


@dataclass(frozen=True)
class InferenceApiSettings:
    feature_api_url: str
    feature_api_timeout_seconds: float
    model_version: str
    shadow_timeout_seconds: float
    shadow_queue_size: int
    shadow_max_concurrency: int

    @classmethod
    def from_env(cls) -> "InferenceApiSettings":
        return cls(
            feature_api_url=os.getenv(
                "FEATURE_API_URL", "http://recsys-online-feature-api"
            ),
            feature_api_timeout_seconds=float(
                os.getenv("FEATURE_API_TIMEOUT_SECONDS", "5")
            ),
            model_version=os.getenv("MODEL_VERSION", "latest"),
            shadow_timeout_seconds=max(1, int_env("AB_SHADOW_TIMEOUT_MS", 1000)) / 1000,
            shadow_queue_size=max(1, int_env("AB_SHADOW_QUEUE_SIZE", 100)),
            shadow_max_concurrency=max(1, int_env("AB_SHADOW_MAX_CONCURRENCY", 4)),
        )
