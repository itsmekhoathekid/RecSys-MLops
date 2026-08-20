"""Client for recsys-online-feature-api."""

from __future__ import annotations

import httpx

from recsys_feature_rag_mcp.clients.base import JsonApiClient


class OnlineFeatureClient(JsonApiClient):
    """Typed transport adapter for recsys-online-feature-api."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        max_connections: int = 50,
        max_keepalive_connections: int = 20,
    ) -> None:
        super().__init__(
            service="recsys-online-feature-api",
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            transport=transport,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )

    async def get_features(
        self,
        *,
        user_id: int,
        candidate_item_ids: list[int] | None,
        top_k: int,
    ) -> dict[str, object]:
        """Fetch materialized features for a user and optional candidates."""

        return await self.request(
            "POST",
            "/online-features",
            json={
                "user_id": user_id,
                "candidate_item_ids": candidate_item_ids,
                "top_k": top_k,
            },
        )
