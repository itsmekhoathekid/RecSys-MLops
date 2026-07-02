from __future__ import annotations

import os
import time

import httpx

from api_schemas import OnlineFeaturesRequest, OnlineFeaturesResponse
from observability import METRICS, span


class OnlineFeatureServiceClient:
    def __init__(self, base_url: str | None = None, timeout_seconds: float | None = None) -> None:
        self.base_url = (base_url or os.getenv("FEATURE_API_URL", "http://recsys-online-feature-api")).rstrip("/")
        self.timeout_seconds = timeout_seconds or float(os.getenv("FEATURE_API_TIMEOUT_SECONDS", "5"))

    async def fetch(self, request: OnlineFeaturesRequest) -> OnlineFeaturesResponse:
        start = time.perf_counter()
        status = "error"
        try:
            with span("feature_api.fetch_online_features", user_id=request.user_id, top_k=request.top_k):
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(
                        f"{self.base_url}/online-features",
                        json=request.model_dump(exclude_none=True),
                    )
            response.raise_for_status()
            status = "success"
            return OnlineFeaturesResponse.model_validate(response.json())
        finally:
            METRICS.observe(
                "recsys_feature_api_client_request_duration_seconds",
                time.perf_counter() - start,
                labels={"status": status},
            )
