"""Client for exact and semantic recsys-rag-api retrieval."""

from __future__ import annotations

import httpx

from recsys_feature_rag_mcp.clients.base import JsonApiClient


class RagClient(JsonApiClient):
    """Typed transport adapter for exact and semantic RAG endpoints."""

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
            service="recsys-rag-api",
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            transport=transport,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )

    async def get_chunk(self, chunk_id: str) -> dict[str, object]:
        """Fetch one exact chunk from the active RAG FeatureView."""

        return await self.request("GET", f"/v1/rag/chunks/{chunk_id}")

    async def retrieve(
        self,
        *,
        query: str,
        top_k_items: int,
        filters: dict[str, object] | None,
    ) -> dict[str, object]:
        """Retrieve semantic evidence grouped by item."""

        return await self.request(
            "POST",
            "/v1/rag/retrieve",
            json={
                "query": query,
                "top_k_items": top_k_items,
                "filters": filters or {},
            },
        )
