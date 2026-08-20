"""Shared async JSON client with one bounded retry."""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx
from opentelemetry.propagate import inject

from recsys_feature_rag_mcp.errors import DownstreamError
from recsys_feature_rag_mcp.observability import (
    DOWNSTREAM_DURATION,
    DOWNSTREAM_REQUESTS,
)


class JsonApiClient:
    """Pool HTTP connections and normalize bounded-retry JSON requests."""

    def __init__(
        self,
        *,
        service: str,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service = service
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Perform one request with a jittered retry for transient failures."""

        headers = dict(kwargs.pop("headers", {}))
        inject(headers)
        kwargs["headers"] = headers
        last_error: DownstreamError | None = None
        for attempt in range(2):
            try:
                with DOWNSTREAM_DURATION.labels(self.service).time():
                    response = await self.client.request(method, path, **kwargs)
                DOWNSTREAM_REQUESTS.labels(
                    self.service, str(response.status_code)
                ).inc()
                if response.status_code in {502, 503} and attempt == 0:
                    await asyncio.sleep(random.uniform(0.04, 0.08))
                    continue
                if response.status_code >= 400:
                    raise DownstreamError(
                        code=f"http_{response.status_code}",
                        service=self.service,
                        retryable=response.status_code in {429, 502, 503, 504},
                        message=f"{self.service} returned HTTP {response.status_code}",
                    )
                return response.json()
            except DownstreamError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = DownstreamError(
                    code="timeout" if isinstance(exc, httpx.TimeoutException) else "network",
                    service=self.service,
                    retryable=True,
                    message=f"{self.service} request failed",
                )
                if attempt == 0:
                    await asyncio.sleep(random.uniform(0.04, 0.08))
                    continue
        assert last_error is not None
        DOWNSTREAM_REQUESTS.labels(self.service, "transport_error").inc()
        raise last_error

    async def aclose(self) -> None:
        """Close the pooled HTTP transport."""

        await self.client.aclose()
