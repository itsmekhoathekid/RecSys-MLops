"""Environment-backed recommendation MCP runtime settings."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RecommendationMcpSettings:
    """Keep transport and authentication configuration outside tool logic."""

    inference_api_url: str
    auth_token: str
    allowed_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    request_timeout_seconds: float = 7.0
    total_deadline_seconds: float = 15.0
    image_reference: str = "unknown"
    downstream_max_connections: int = 50
    downstream_max_keepalive_connections: int = 20

    @classmethod
    def from_env(cls) -> RecommendationMcpSettings:
        """Load settings injected by the Kubernetes chart."""

        origins = tuple(
            value.strip()
            for value in os.getenv(
                "MCP_ALLOWED_ORIGINS", "http://localhost,http://127.0.0.1"
            ).split(",")
            if value.strip()
        )
        hosts = tuple(
            value.strip()
            for value in os.getenv(
                "MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*,[::1]:*"
            ).split(",")
            if value.strip()
        )
        return cls(
            inference_api_url=os.getenv(
                "INFERENCE_API_URL",
                "http://recsys-inference-api.api-serving.svc.cluster.local",
            ).rstrip("/"),
            auth_token=os.getenv("MCP_AUTH_TOKEN", ""),
            allowed_origins=origins,
            allowed_hosts=hosts,
            request_timeout_seconds=float(
                os.getenv("INFERENCE_REQUEST_TIMEOUT_SECONDS", "7")
            ),
            total_deadline_seconds=float(
                os.getenv("INFERENCE_TOTAL_DEADLINE_SECONDS", "15")
            ),
            image_reference=os.getenv("RECSYS_IMAGE_REFERENCE", "unknown"),
            downstream_max_connections=int(
                os.getenv("DOWNSTREAM_MAX_CONNECTIONS", "50")
            ),
            downstream_max_keepalive_connections=int(
                os.getenv("DOWNSTREAM_MAX_KEEPALIVE_CONNECTIONS", "20")
            ),
        )
