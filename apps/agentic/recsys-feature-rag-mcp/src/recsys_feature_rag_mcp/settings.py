"""Environment-backed MCP server settings."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class McpSettings:
    """Runtime settings kept independent from Kubernetes implementation details."""

    online_feature_api_url: str
    rag_api_url: str
    auth_token: str
    allowed_origins: tuple[str, ...]
    online_feature_timeout_seconds: float = 2.0
    rag_timeout_seconds: float = 5.0
    image_reference: str = "unknown"

    @classmethod
    def from_env(cls) -> McpSettings:
        """Load settings from the Kubernetes-injected environment."""

        origins = tuple(
            origin.strip()
            for origin in os.getenv(
                "MCP_ALLOWED_ORIGINS",
                "http://localhost,http://127.0.0.1",
            ).split(",")
            if origin.strip()
        )
        return cls(
            online_feature_api_url=os.getenv(
                "ONLINE_FEATURE_API_URL",
                "http://recsys-online-feature-api.api-serving.svc.cluster.local",
            ).rstrip("/"),
            rag_api_url=os.getenv(
                "RAG_API_URL",
                "http://recsys-rag-api.api-serving.svc.cluster.local",
            ).rstrip("/"),
            auth_token=os.getenv("MCP_AUTH_TOKEN", ""),
            allowed_origins=origins,
            online_feature_timeout_seconds=float(
                os.getenv("ONLINE_FEATURE_TIMEOUT_SECONDS", "2")
            ),
            rag_timeout_seconds=float(os.getenv("RAG_TIMEOUT_SECONDS", "5")),
            image_reference=os.getenv("RECSYS_IMAGE_REFERENCE", "unknown"),
        )
