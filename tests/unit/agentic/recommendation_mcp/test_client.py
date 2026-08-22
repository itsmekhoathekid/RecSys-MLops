from __future__ import annotations

import asyncio

import httpx
import pytest
from recsys_recommendation_mcp.client import InferenceApiClient
from recsys_recommendation_mcp.errors import DownstreamError


def response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "user_id": 1001,
            "model_version": "bst-v7",
            "ab_variant": "control",
            "ab_experiment_id": "ranking-2026",
            "items": [
                {"item_id": 2, "score": 0.9},
                {"item_id": 1, "score": 0.8},
            ],
        },
        request=request,
    )


@pytest.mark.asyncio
async def test_public_request_contract_and_connection_pool_concurrency() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response(request)

    client = InferenceApiClient(
        base_url="http://inference",
        request_timeout_seconds=2,
        total_deadline_seconds=15,
        transport=httpx.MockTransport(handler),
    )
    try:
        results = await asyncio.gather(
            *[
                client.recommend(user_id=1001, candidate_item_ids=None, top_k=10)
                for _ in range(4)
            ]
        )
    finally:
        await client.aclose()

    assert all([item.item_id for item in result.items] == [2, 1] for result in results)
    assert all(request.url.path == "/recommendations" for request in captured)
    assert all("candidate_item_ids" not in request.content.decode() for request in captured)


@pytest.mark.asyncio
async def test_candidates_are_forwarded_exactly_and_503_retries_once() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 1:
            return httpx.Response(503, request=request)
        return response(request)

    client = InferenceApiClient(
        base_url="http://inference",
        request_timeout_seconds=2,
        total_deadline_seconds=15,
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.recommend(
            user_id=1001, candidate_item_ids=[800080, 800081], top_k=2
        )
    finally:
        await client.aclose()
    assert len(captured) == 2
    assert captured[-1].content == (
        b'{"user_id":1001,"top_k":2,"candidate_item_ids":[800080,800081]}'
    )


@pytest.mark.asyncio
async def test_4xx_is_non_retryable() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(422, request=request)

    client = InferenceApiClient(
        base_url="http://inference",
        request_timeout_seconds=2,
        total_deadline_seconds=15,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DownstreamError) as caught:
            await client.recommend(user_id=1001, candidate_item_ids=None, top_k=10)
    finally:
        await client.aclose()
    assert requests == 1
    assert caught.value.code == "http_422"
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_connect_error_retries_once_then_succeeds() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary", request=request)
        return response(request)

    client = InferenceApiClient(
        base_url="http://inference",
        request_timeout_seconds=2,
        total_deadline_seconds=15,
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.recommend(user_id=1001, candidate_item_ids=None, top_k=10)
    finally:
        await client.aclose()
    assert attempts == 2


@pytest.mark.asyncio
async def test_timeout_exhausts_one_retry_and_is_typed() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("slow", request=request)

    client = InferenceApiClient(
        base_url="http://inference",
        request_timeout_seconds=0.01,
        total_deadline_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DownstreamError) as caught:
            await client.recommend(user_id=1001, candidate_item_ids=None, top_k=10)
    finally:
        await client.aclose()
    assert attempts == 2
    assert caught.value.code == "timeout"
    assert caught.value.retryable is True
