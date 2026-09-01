from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from recsys_rag_api.app import create_app
from recsys_rag_api.contracts import ChunkBatchResponse, RetrievalResponse

from .conftest import DeterministicChunkService, rag_settings


@pytest.mark.parametrize(
    ("payload", "expected_query"),
    [
        ({"query": "tai nghe", "top_k_items": 10}, "tai nghe"),
        (
            {"query": "tai nghe", "filters": {"brands": [" Sony "]}},
            "tai nghe",
        ),
        ({"query": "  tai nghe  "}, "tai nghe"),
    ],
    ids=["ep-no-filter", "ep-filtered", "ep-normalized-query"],
)
def test_retrieve_equivalence_partitions(
    rag_api: TestClient, payload: dict, expected_query: str
) -> None:
    response = rag_api.post("/v1/rag/retrieve", json=payload)

    assert response.status_code == 200
    assert response.json()["query"] == expected_query
    rag_api.app.state.retrieval_service_mock.retrieve.assert_called_once()
    request = rag_api.app.state.retrieval_service_mock.retrieve.call_args.args[0]
    assert request.query == expected_query
    if "brands" in payload.get("filters", {}):
        assert request.filters.brands == ["Sony"]


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"query": "", "top_k_items": 1}, 422),
        ({"query": "x", "top_k_items": 1}, 200),
        ({"query": "x" * 1000, "top_k_items": 20}, 200),
        ({"query": "x" * 1001, "top_k_items": 1}, 422),
        ({"query": "x", "top_k_items": 0}, 422),
        ({"query": "x", "top_k_items": 1}, 200),
        ({"query": "x", "top_k_items": 20}, 200),
        ({"query": "x", "top_k_items": 21}, 422),
    ],
    ids=[
        "query-min-minus-one",
        "query-min",
        "query-max",
        "query-max-plus-one",
        "top-k-min-minus-one",
        "top-k-min",
        "top-k-max",
        "top-k-max-plus-one",
    ],
)
def test_retrieve_boundary_value_analysis(
    rag_api: TestClient, payload: dict, expected_status: int
) -> None:
    response = rag_api.post("/v1/rag/retrieve", json=payload)

    assert response.status_code == expected_status
    service = rag_api.app.state.retrieval_service_mock
    if expected_status == 200:
        service.retrieve.assert_called_once()
    else:
        service.retrieve.assert_not_called()


@pytest.mark.parametrize(
    ("filters", "expected_status", "expected"),
    [
        ({"brands": [" Sony ", "Acme"]}, 200, {"brands": ["Sony", "Acme"]}),
        ({"brands": []}, 422, None),
        ({"brands": [" "]}, 422, None),
        ({"category_prefix": []}, 200, {"category_prefix": []}),
        ({"category_prefix": ["Audio"]}, 200, {"category_prefix": ["Audio"]}),
        (
            {"category_prefix": ["Audio", "Headphones", "Wireless"]},
            200,
            {"category_prefix": ["Audio", "Headphones", "Wireless"]},
        ),
        ({"category_prefix": ["a", "b", "c", "d"]}, 422, None),
        ({"min_current_price": -0.01}, 422, None),
        ({"min_current_price": 0}, 200, {"min_current_price": 0}),
        ({"max_current_price": -0.01}, 422, None),
        ({"max_current_price": 0}, 200, {"max_current_price": 0}),
        (
            {"min_current_price": 20, "max_current_price": 10},
            200,
            {"min_current_price": 20, "max_current_price": 10},
        ),
        ({"in_stock": True}, 200, {"in_stock": True}),
        ({"in_stock": False}, 200, {"in_stock": False}),
        ({"chunk_types": ["review", "qna"]}, 200, {"chunk_types": ["review", "qna"]}),
        ({"chunk_types": []}, 200, {"chunk_types": []}),
        ({"chunk_types": ["invalid"]}, 422, None),
        ({"unknown_filter": "forbidden"}, 422, None),
    ],
    ids=[
        "brand-normalization",
        "brand-empty-list",
        "brand-blank",
        "category-depth-zero",
        "category-depth-one",
        "category-depth-three",
        "category-depth-four",
        "min-price-negative-epsilon",
        "min-price-zero",
        "max-price-negative-epsilon",
        "max-price-zero",
        "price-min-greater-than-max-current-contract",
        "stock-true",
        "stock-false",
        "valid-chunk-types",
        "empty-chunk-types-current-contract",
        "invalid-chunk-type",
        "forbidden-filter-field",
    ],
)
def test_retrieve_filter_partitions_and_boundaries(
    rag_api: TestClient,
    filters: dict,
    expected_status: int,
    expected: dict | None,
) -> None:
    response = rag_api.post(
        "/v1/rag/retrieve", json={"query": "headphones", "filters": filters}
    )

    assert response.status_code == expected_status
    service = rag_api.app.state.retrieval_service_mock
    if expected_status == 200:
        service.retrieve.assert_called_once()
        request_filters = service.retrieve.call_args.args[0].filters.model_dump(
            exclude_none=True
        )
        for key, value in (expected or {}).items():
            assert request_filters[key] == value
    else:
        service.retrieve.assert_not_called()


def test_retrieve_forbids_extra_top_level_fields(rag_api: TestClient) -> None:
    response = rag_api.post(
        "/v1/rag/retrieve", json={"query": "headphones", "unknown": True}
    )
    assert response.status_code == 422
    rag_api.app.state.retrieval_service_mock.retrieve.assert_not_called()


@pytest.mark.parametrize(
    ("chunk_id", "expected_status"),
    [
        ("a", 200),
        ("x" * 512, 200),
        ("x" * 513, 422),
        ("missing", 404),
    ],
    ids=["path-min", "path-max", "path-max-plus-one", "missing-partition"],
)
def test_single_chunk_partitions_and_boundaries(
    rag_api: TestClient, chunk_id: str, expected_status: int
) -> None:
    response = rag_api.get(f"/v1/rag/chunks/{chunk_id}")

    assert response.status_code == expected_status
    service = rag_api.app.state.chunk_service_mock
    if expected_status == 422:
        service.get_many.assert_not_called()
    else:
        service.get_many.assert_called_once_with([chunk_id])


def test_single_chunk_empty_path_is_not_an_endpoint(rag_api: TestClient) -> None:
    response = rag_api.get("/v1/rag/chunks/")
    assert response.status_code == 404
    rag_api.app.state.chunk_service_mock.get_many.assert_not_called()


@pytest.mark.parametrize(
    ("chunk_ids", "expected_status"),
    [
        ([], 422),
        (["only"], 200),
        ([f"chunk-{index}" for index in range(100)], 200),
        ([f"chunk-{index}" for index in range(101)], 422),
        (["same", "same"], 422),
        (["valid", "   "], 422),
    ],
    ids=[
        "count-min-minus-one",
        "count-min",
        "count-max",
        "count-max-plus-one",
        "duplicate-partition",
        "blank-partition",
    ],
)
def test_batch_partitions_and_boundaries(
    rag_api: TestClient, chunk_ids: list[str], expected_status: int
) -> None:
    response = rag_api.post("/v1/rag/chunks:batch-get", json={"chunk_ids": chunk_ids})

    assert response.status_code == expected_status
    service = rag_api.app.state.chunk_service_mock
    if expected_status == 200:
        service.get_many.assert_called_once_with(chunk_ids)
    else:
        service.get_many.assert_not_called()


def test_batch_normalizes_preserves_order_and_reports_missing(
    rag_api: TestClient,
) -> None:
    response = rag_api.post(
        "/v1/rag/chunks:batch-get",
        json={"chunk_ids": [" chunk-2 ", "missing", "chunk-3"]},
    )

    assert response.status_code == 200
    assert [chunk["chunk_id"] for chunk in response.json()["chunks"]] == [
        "chunk-2",
        "chunk-3",
    ]
    assert response.json()["missing_chunk_ids"] == ["missing"]
    rag_api.app.state.chunk_service_mock.get_many.assert_called_once_with(
        ["chunk-2", "missing", "chunk-3"]
    )


@given(
    query=st.text(
        alphabet=st.sampled_from(list("abc xyzđiệntử")), min_size=1, max_size=40
    ).filter(lambda value: bool(value.strip())),
    top_k_items=st.integers(min_value=1, max_value=20),
    in_stock=st.one_of(st.none(), st.booleans()),
)
@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_retrieve_http_idempotency(
    rag_api: TestClient,
    query: str,
    top_k_items: int,
    in_stock: bool | None,
) -> None:
    retrieval = rag_api.app.state.retrieval_service_mock
    retrieval.reset_mock()
    rag_api.app.state.encoder.calls.clear()
    rag_api.app.state.search.calls.clear()
    rag_api.app.state.pointers.calls = 0
    payload: dict = {"query": query, "top_k_items": top_k_items}
    if in_stock is not None:
        payload["filters"] = {"in_stock": in_stock}

    responses = [rag_api.post("/v1/rag/retrieve", json=payload) for _ in range(3)]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert responses[0].json() == responses[1].json() == responses[2].json()
    assert retrieval.retrieve.call_count == 3
    assert len(rag_api.app.state.encoder.calls) == 3
    assert rag_api.app.state.pointers.calls == 3
    assert len(rag_api.app.state.search.calls) >= 3


safe_chunk_ids = st.text(
    alphabet=st.sampled_from(list("abcdefghijklmnopqrstuvwxyz0123456789-_")),
    min_size=1,
    max_size=60,
)


@given(chunk_id=safe_chunk_ids)
@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_single_chunk_http_idempotency(rag_api: TestClient, chunk_id: str) -> None:
    chunks = rag_api.app.state.chunk_service_mock
    chunks.reset_mock()

    responses = [rag_api.get(f"/v1/rag/chunks/{chunk_id}") for _ in range(3)]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert responses[0].json() == responses[1].json() == responses[2].json()
    assert chunks.get_many.call_count == 3
    assert all(call.args == ([chunk_id],) for call in chunks.get_many.call_args_list)


@given(chunk_ids=st.lists(safe_chunk_ids, min_size=1, max_size=20, unique=True))
@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_batch_get_http_idempotency(rag_api: TestClient, chunk_ids: list[str]) -> None:
    chunks = rag_api.app.state.chunk_service_mock
    chunks.reset_mock()
    payload = {"chunk_ids": chunk_ids}

    responses = [
        rag_api.post("/v1/rag/chunks:batch-get", json=payload) for _ in range(3)
    ]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert responses[0].json() == responses[1].json() == responses[2].json()
    assert chunks.get_many.call_count == 3
    assert all(call.args == (chunk_ids,) for call in chunks.get_many.call_args_list)


class MinimalRetrievalService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.pointers = SimpleNamespace(get=lambda: object())

    def retrieve(self, request):
        if self.error is not None:
            raise self.error
        return RetrievalResponse(query=request.query, pipeline_run_id="run", items=[])


class ErrorChunkService:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def get_many(self, chunk_ids: list[str]) -> ChunkBatchResponse:
        raise self.error


@pytest.mark.parametrize(
    ("endpoint", "error", "expected_status"),
    [
        ("single", RuntimeError("pointer"), 503),
        ("single", ValueError("feast"), 502),
        ("batch", RuntimeError("pointer"), 503),
        ("batch", ValueError("feast"), 502),
    ],
)
def test_chunk_dependency_errors_are_classified(
    endpoint: str, error: Exception, expected_status: int
) -> None:
    app = create_app(
        rag_settings(),
        retrieval_service=MinimalRetrievalService(),
        chunk_lookup_service=ErrorChunkService(error),
    )
    with TestClient(app) as client:
        if endpoint == "single":
            response = client.get("/v1/rag/chunks/chunk-1")
        else:
            response = client.post(
                "/v1/rag/chunks:batch-get", json={"chunk_ids": ["chunk-1"]}
            )
    assert response.status_code == expected_status


def test_missing_chunk_service_returns_503() -> None:
    with TestClient(
        create_app(rag_settings(), retrieval_service=MinimalRetrievalService())
    ) as client:
        assert client.get("/v1/rag/chunks/chunk-1").status_code == 503
        assert (
            client.post(
                "/v1/rag/chunks:batch-get", json={"chunk_ids": ["chunk-1"]}
            ).status_code
            == 503
        )


def test_retrieval_dependency_error_returns_bad_gateway() -> None:
    with TestClient(
        create_app(
            rag_settings(),
            retrieval_service=MinimalRetrievalService(ValueError("search")),
            chunk_lookup_service=DeterministicChunkService(),
        )
    ) as client:
        response = client.post("/v1/rag/retrieve", json={"query": "headphones"})
    assert response.status_code == 502
