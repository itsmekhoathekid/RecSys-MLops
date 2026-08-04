from __future__ import annotations

import os
from dataclasses import dataclass

from recsys_serving_common.env import bool_env


@dataclass(frozen=True)
class FeatureApiSettings:
    warmup_on_startup: bool
    allow_fallback: bool = False
    feast_repo_path: str = "/opt/recsys/apps/data-platform/feature-store/feature_repo"
    feast_runtime_repo_path: str = "/tmp/recsys-feast-feature-repo"
    feast_apply_on_startup: bool = False
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    feast_redis_connection_string: str = ""

    @classmethod
    def from_env(cls) -> "FeatureApiSettings":
        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_port = int(os.getenv("REDIS_PORT", "6379"))
        return cls(
            warmup_on_startup=bool_env("FEATURE_API_WARMUP_ON_STARTUP", "1"),
            allow_fallback=bool_env("ALLOW_FEATURE_FALLBACK"),
            feast_repo_path=os.getenv("FEAST_REPO_PATH", cls.feast_repo_path),
            feast_runtime_repo_path=os.getenv(
                "FEAST_RUNTIME_REPO_PATH", cls.feast_runtime_repo_path
            ),
            feast_apply_on_startup=bool_env("FEAST_APPLY_ON_STARTUP"),
            redis_host=redis_host,
            redis_port=redis_port,
            redis_db=int(os.getenv("REDIS_DB", "0")),
            feast_redis_connection_string=os.getenv(
                "FEAST_REDIS_CONNECTION_STRING", f"{redis_host}:{redis_port}"
            ),
        )
