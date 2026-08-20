from __future__ import annotations

import os
import time

import httpx

from recsys_serving_common.contracts import (
    OnlineFeaturesRequest,
    OnlineFeaturesResponse,
)
from recsys_serving_common.observability import METRICS, span


class OnlineFeatureServiceClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.getenv("FEATURE_API_URL")
            or "http://recsys-online-feature-api"
        ).rstrip("/")
        self.timeout_seconds = timeout_seconds or float(
            os.getenv("FEATURE_API_TIMEOUT_SECONDS", "5")
        )
        self.client = client

    async def fetch(self, request: OnlineFeaturesRequest) -> OnlineFeaturesResponse:
        start = time.perf_counter()
        status = "error"
        payload = request.model_dump(exclude_none=True)

        async def fetch_response(client: httpx.AsyncClient) -> httpx.Response:
            if request.candidate_item_ids is None:
                return await client.get(
                    f"{self.base_url}/online-features/{request.user_id}",
                    params={"top_k": request.top_k},
                )
            return await client.post(
                f"{self.base_url}/online-features",
                json=payload,
            )

        try:
            with span(
                "feature_api.fetch_online_features",
                user_id=request.user_id,
                top_k=request.top_k,
            ):
                if self.client is None:
                    async with httpx.AsyncClient(
                        timeout=self.timeout_seconds
                    ) as client:
                        response = await fetch_response(client)
                else:
                    response = await fetch_response(self.client)
            response.raise_for_status()
            status = "success"
            return OnlineFeaturesResponse.model_validate(response.json())
        finally:
            METRICS.observe(
                "recsys_feature_api_client_request_duration_seconds",
                time.perf_counter() - start,
                labels={"status": status},
            )
