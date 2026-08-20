from __future__ import annotations

import httpx
import pytest
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, use_span
from recsys_feature_rag_mcp.clients.online_features import OnlineFeatureClient
from recsys_feature_rag_mcp.clients.rag import RagClient
from recsys_feature_rag_mcp.errors import DownstreamError


@pytest.mark.asyncio
async def test_clients_send_public_api_contracts_and_retry_503_once():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/online-features":
            return httpx.Response(200, json={"user_id": 7}, request=request)
        if request.url.path == "/v1/rag/retrieve" and len(
            [item for item in requests if item.url.path == request.url.path]
        ) == 1:
            return httpx.Response(503, json={"detail": "warming"}, request=request)
        return httpx.Response(200, json={"pipeline_run_id": "run-1"}, request=request)

    transport = httpx.MockTransport(handler)
    feature = OnlineFeatureClient(
        base_url="http://feature", timeout_seconds=2, transport=transport
    )
    rag = RagClient(base_url="http://rag", timeout_seconds=5, transport=transport)
    try:
        assert (
            await feature.get_features(
                user_id=7, candidate_item_ids=[1, 2], top_k=2
            )
        )["user_id"] == 7
        assert (
            await rag.retrieve(query="headphones", top_k_items=3, filters={})
        )["pipeline_run_id"] == "run-1"
        assert (await rag.get_chunk("chunk-1"))["pipeline_run_id"] == "run-1"
    finally:
        await feature.aclose()
        await rag.aclose()

    rag_requests = [item for item in requests if item.url.path == "/v1/rag/retrieve"]
    assert len(rag_requests) == 2


@pytest.mark.asyncio
async def test_client_classifies_non_retryable_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "missing"}, request=request)

    rag = RagClient(
        base_url="http://rag",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DownstreamError) as caught:
            await rag.get_chunk("missing")
    finally:
        await rag.aclose()
    assert caught.value.code == "http_404"
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_client_retries_one_connect_error_then_succeeds():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary failure", request=request)
        return httpx.Response(200, json={"chunk_id": "chunk-1"}, request=request)

    rag = RagClient(
        base_url="http://rag",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert (await rag.get_chunk("chunk-1"))["chunk_id"] == "chunk-1"
    finally:
        await rag.aclose()
    assert attempts == 2


@pytest.mark.asyncio
async def test_client_propagates_the_current_w3c_trace_context():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"user_id": 7}, request=request)

    feature = OnlineFeatureClient(
        base_url="http://feature",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
    )
    span = NonRecordingSpan(
        SpanContext(
            trace_id=0x1234567890ABCDEF1234567890ABCDEF,
            span_id=0x1234567890ABCDEF,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    )
    try:
        with use_span(span):
            await feature.get_features(
                user_id=7,
                candidate_item_ids=None,
                top_k=10,
            )
    finally:
        await feature.aclose()

    assert captured[0].headers["traceparent"].startswith(
        "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
    )
