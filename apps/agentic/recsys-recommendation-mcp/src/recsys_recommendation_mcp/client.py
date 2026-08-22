"""Pooled async client for the existing recommendation inference API."""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx
from opentelemetry.propagate import inject

from recsys_recommendation_mcp.contracts import RecommendationResponse
from recsys_recommendation_mcp.errors import DownstreamError
from recsys_recommendation_mcp.observability import (
    DOWNSTREAM_DURATION,
    DOWNSTREAM_REQUESTS,
    RETRIES,
)
from recsys_recommendation_mcp.policy import build_request_payload, is_retryable_status


class InferenceApiClient:
    """Call only the public inference API with one bounded transient retry."""

    def __init__(
        self,
        *,
        base_url: str,
        request_timeout_seconds: float,
        total_deadline_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        max_connections: int = 50,
        max_keepalive_connections: int = 20,
    ) -> None:
        self.total_deadline_seconds = total_deadline_seconds
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(request_timeout_seconds),
            transport=transport,
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
            ),
        )

    async def recommend(
        self,
        *,
        user_id: int,
        candidate_item_ids: list[int] | None,
        top_k: int,
    ) -> RecommendationResponse:
        """Return the validated inference response without changing order or scores."""

        payload = build_request_payload(user_id, candidate_item_ids, top_k)
        try:
            async with asyncio.timeout(self.total_deadline_seconds):
                return await self._recommend_with_retry(payload)
        except TimeoutError as exc:
            DOWNSTREAM_REQUESTS.labels("deadline_exceeded").inc()
            raise DownstreamError(
                code="timeout",
                service="recsys-inference-api",
                retryable=True,
                message="recommendation service deadline exceeded",
            ) from exc

    async def _recommend_with_retry(
        self, payload: dict[str, Any]
    ) -> RecommendationResponse:
        """Retry one transient transport or upstream failure with short jitter."""

        last_error: DownstreamError | None = None
        headers: dict[str, str] = {}
        inject(headers)
        for attempt in range(2):
            try:
                with DOWNSTREAM_DURATION.time():
                    response = await self.client.post(
                        "/recommendations", json=payload, headers=headers
                    )
                DOWNSTREAM_REQUESTS.labels(str(response.status_code)).inc()
                if is_retryable_status(response.status_code) and attempt == 0:
                    RETRIES.labels(f"http_{response.status_code}").inc()
                    await asyncio.sleep(random.uniform(0.04, 0.08))
                    continue
                if response.status_code >= 400:
                    raise DownstreamError(
                        code=f"http_{response.status_code}",
                        service="recsys-inference-api",
                        retryable=is_retryable_status(response.status_code),
                        message=(
                            "recommendation service returned "
                            f"HTTP {response.status_code}"
                        ),
                    )
                return RecommendationResponse.model_validate(response.json())
            except DownstreamError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                reason = (
                    "timeout" if isinstance(exc, httpx.TimeoutException) else "network"
                )
                DOWNSTREAM_REQUESTS.labels(reason).inc()
                last_error = DownstreamError(
                    code=reason,
                    service="recsys-inference-api",
                    retryable=True,
                    message="recommendation service request failed",
                )
                if attempt == 0:
                    RETRIES.labels(reason).inc()
                    await asyncio.sleep(random.uniform(0.04, 0.08))
                    continue
        assert last_error is not None
        raise last_error

    async def aclose(self) -> None:
        """Close the shared keep-alive transport."""

        await self.client.aclose()
