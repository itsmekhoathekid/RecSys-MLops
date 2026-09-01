from __future__ import annotations

import json
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

import recsys_rag_api.batching as batching_module
from recsys_rag_api.batching import BatchingTextEncoder, _EncodeRequest
from recsys_rag_api.chunk_lookup import (
    CHUNK_FEATURES,
    ChunkLookupService,
    _as_bool,
    _column_value,
)
from recsys_rag_api.contracts import (
    CandidateChunk,
    ChunkBatchRequest,
    RetrievalFilters,
    RetrievalRequest,
)
from recsys_rag_api.pointer import (
    ActivePointer,
    ActivePointerManager,
    EmbeddingContract,
)
from recsys_rag_api.retrieval import MilvusCandidateSearch, RetrievalService, _matches


REVISION = "03415a4be176a1620747c692ed433219fabc3def"
REAL_THREAD = threading.Thread


def pointer_bytes(run: str = "run-1", revision: str = REVISION) -> bytes:
    return json.dumps(
        {
            "active_slot": "blue",
            "feature_view": "rag_item_chunks_blue",
            "pipeline_run_id": run,
            "source_run_id": "source",
            "chunker_version": "semantic_chunker_v1",
            "embedding_model": "intfloat/multilingual-e5-small",
            "embedding_revision": revision,
            "embedding_dimension": 384,
        }
    ).encode()


def manager(loader=lambda: pointer_bytes(), reload_seconds: float = 0):
    return ActivePointerManager(
        loader=loader,
        supported_contracts=[
            EmbeddingContract("intfloat/multilingual-e5-small", REVISION, 384)
        ],
        reload_seconds=reload_seconds,
    )


def candidate(
    item_id: int,
    chunk_id: str,
    score: float,
    *,
    rating: float = 4.5,
    brand: str = "Sony",
    stock: bool = True,
) -> CandidateChunk:
    return CandidateChunk(
        chunk_id=chunk_id,
        item_id=item_id,
        chunk_type="review",
        source_key=f"reviews/{chunk_id}",
        text=f"evidence {chunk_id}",
        brand=brand,
        category_l1="Electronics",
        category_l2="Audio",
        category_l3="Headphones",
        current_price=20.0 + item_id,
        in_stock=stock,
        average_rating=rating,
        score=score,
    )


def test_strict_contract_boundaries_and_normalization() -> None:
    assert RetrievalRequest(query=" x ").query == "x"
    assert RetrievalRequest(query="x").top_k_items == 10
    assert RetrievalRequest(query="x" * 1000, top_k_items=20).top_k_items == 20
    assert RetrievalFilters(brands=[" Sony "]).brands == ["Sony"]
    assert RetrievalFilters(category_prefix=["a", "b", "c"]).category_prefix == [
        "a",
        "b",
        "c",
    ]
    for payload in (
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 1001},
        {"query": "x", "top_k_items": 0},
        {"query": "x", "top_k_items": 21},
        {"query": "x", "extra": True},
    ):
        with pytest.raises(ValidationError):
            RetrievalRequest.model_validate(payload)
    for filters in (
        {"brands": []},
        {"brands": [""]},
        {"category_prefix": ["a", "b", "c", "d"]},
        {"min_current_price": -0.01},
        {"max_current_price": -0.01},
        {"chunk_types": ["invalid"]},
        {"extra": True},
    ):
        with pytest.raises(ValidationError):
            RetrievalFilters.model_validate(filters)

    assert ChunkBatchRequest(chunk_ids=[" a ", "b"]).chunk_ids == ["a", "b"]
    for chunk_ids in ([], ["a"] * 101, ["a", "a"], [" "]):
        with pytest.raises(ValidationError):
            ChunkBatchRequest(chunk_ids=chunk_ids)


def test_pointer_contract_support_and_last_known_good() -> None:
    pointer = ActivePointer.model_validate_json(pointer_bytes())
    supported = EmbeddingContract("intfloat/multilingual-e5-small", REVISION, 384)
    assert supported.supports(pointer) is True
    assert EmbeddingContract("other", REVISION, 384).supports(pointer) is False
    assert EmbeddingContract(supported.model, "other", 384).supports(pointer) is False
    assert EmbeddingContract(supported.model, REVISION, 1).supports(pointer) is False

    payloads = iter([pointer_bytes(), pointer_bytes(run="bad", revision="unsupported")])
    pointers = manager(lambda: next(payloads))
    assert pointers.get().pipeline_run_id == "run-1"
    assert pointers.get().pipeline_run_id == "run-1"

    cold = manager(lambda: b"not-json")
    with pytest.raises(RuntimeError, match="No valid active RAG index pointer"):
        cold.get()


def test_pointer_cache_honors_reload_ttl() -> None:
    calls = []

    def loader():
        calls.append(1)
        return pointer_bytes()

    pointers = manager(loader, reload_seconds=3600)
    assert pointers.get().pipeline_run_id == "run-1"
    assert pointers.get().pipeline_run_id == "run-1"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("filters", "matches"),
    [
        ({}, True),
        ({"brands": ["SONY"]}, True),
        ({"brands": ["Other"]}, False),
        ({"category_prefix": ["Electronics", "Audio"]}, True),
        ({"category_prefix": ["Other"]}, False),
        ({"min_current_price": 21.0}, True),
        ({"min_current_price": 21.01}, False),
        ({"max_current_price": 21.0}, True),
        ({"max_current_price": 20.99}, False),
        ({"in_stock": True}, True),
        ({"in_stock": False}, False),
        ({"chunk_types": ["review"]}, True),
        ({"chunk_types": ["qna"]}, False),
    ],
)
def test_matches_every_filter_boundary(filters: dict, matches: bool) -> None:
    assert _matches(candidate(1, "1:a", 0.9), RetrievalFilters(**filters)) is matches


class Encoder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[1.0, 0.0]]

    def token_count(self, text: str) -> int:
        return len(text.split())


class Search:
    def __init__(self, rows: list[CandidateChunk]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, list[float], int]] = []

    def search(self, *, feature_view: str, query_vector: list[float], top_k: int):
        self.calls.append((feature_view, query_vector, top_k))
        return self.rows[:top_k]


class Pointers:
    def get(self) -> SimpleNamespace:
        return SimpleNamespace(
            feature_view="rag_item_chunks_green", pipeline_run_id="pipeline-9"
        )


def test_retrieval_filters_groups_sorts_caps_and_preserves_contract() -> None:
    encoder = Encoder()
    search = Search(
        [
            candidate(2, "2:a", 0.9, rating=4.6),
            candidate(1, "1:b", 0.8, rating=4.8),
            candidate(1, "1:a", 0.9, rating=4.8),
            candidate(1, "1:c", 0.7, rating=4.8),
            candidate(3, "3:a", 0.99, stock=False),
            candidate(4, "4:a", 0.95, brand="Other"),
        ]
    )
    response = RetrievalService(
        encoder=encoder, search=search, pointers=Pointers()
    ).retrieve(
        RetrievalRequest(
            query="headphones",
            top_k_items=2,
            filters=RetrievalFilters(brands=["sony"], in_stock=True),
        )
    )
    assert response.model_dump() == {
        "query": "headphones",
        "pipeline_run_id": "pipeline-9",
        "items": [
            {
                "item_id": 1,
                "score": 0.9,
                "brand": "Sony",
                "category_path": ["Electronics", "Audio", "Headphones"],
                "current_price": 21.0,
                "in_stock": True,
                "average_rating": 4.8,
                "evidence": [
                    {
                        "chunk_id": "1:a",
                        "chunk_type": "review",
                        "source_key": "reviews/1:a",
                        "text": "evidence 1:a",
                        "score": 0.9,
                    },
                    {
                        "chunk_id": "1:b",
                        "chunk_type": "review",
                        "source_key": "reviews/1:b",
                        "text": "evidence 1:b",
                        "score": 0.8,
                    },
                ],
            },
            {
                "item_id": 2,
                "score": 0.9,
                "brand": "Sony",
                "category_path": ["Electronics", "Audio", "Headphones"],
                "current_price": 22.0,
                "in_stock": True,
                "average_rating": 4.6,
                "evidence": [
                    {
                        "chunk_id": "2:a",
                        "chunk_type": "review",
                        "source_key": "reviews/2:a",
                        "text": "evidence 2:a",
                        "score": 0.9,
                    }
                ],
            },
        ],
    }
    assert encoder.calls == [["query: headphones"]]
    assert search.calls == [("rag_item_chunks_green", [1.0, 0.0], 100)]


def test_retrieval_refills_pool_until_target_and_caps_final_items() -> None:
    rows = [candidate(1, f"1:{index:03}", 0.9) for index in range(120)] + [
        candidate(2, "2:a", 0.8),
        candidate(3, "3:a", 0.7),
    ]
    search = Search(rows)
    response = RetrievalService(
        encoder=Encoder(), search=search, pointers=Pointers()
    ).retrieve(RetrievalRequest(query="query", top_k_items=2))
    assert [item.item_id for item in response.items] == [1, 2]
    assert [call[2] for call in search.calls] == [100, 200]


class Result:
    def __init__(self, columns):
        self.columns = columns

    def to_dict(self):
        return self.columns


class Store:
    def __init__(self):
        self.request = None

    def get_online_features(self, **request):
        self.request = request
        return Result(
            {
                "item_id": [7, None, 9],
                "chunk_type": ["review", None, "qna"],
                "source_key": ["a", None, "c"],
                "text": ["first", None, "third"],
                "brand": ["A", None, "C"],
                "category_l1": ["one", None, "three"],
                "category_l2": ["", None, ""],
                "category_l3": ["", None, ""],
                "current_price": [1.5, None, 3.5],
                "in_stock": ["True", None, "False"],
                "average_rating": [4.2, None, 4.8],
                "source_run_id": ["source", None, "source"],
            }
        )


def test_chunk_lookup_pointer_view_order_missing_and_conversions() -> None:
    store = Store()
    service = ChunkLookupService(feature_store=store, pointers=Pointers())
    result = service.get_many(["a", "missing", "c"])
    assert result.model_dump() == {
        "pipeline_run_id": "pipeline-9",
        "chunks": [
            {
                "chunk_id": "a",
                "item_id": 7,
                "chunk_type": "review",
                "source_key": "a",
                "text": "first",
                "brand": "A",
                "category_l1": "one",
                "category_l2": "",
                "category_l3": "",
                "current_price": 1.5,
                "in_stock": True,
                "average_rating": 4.2,
                "source_run_id": "source",
            },
            {
                "chunk_id": "c",
                "item_id": 9,
                "chunk_type": "qna",
                "source_key": "c",
                "text": "third",
                "brand": "C",
                "category_l1": "three",
                "category_l2": "",
                "category_l3": "",
                "current_price": 3.5,
                "in_stock": False,
                "average_rating": 4.8,
                "source_run_id": "source",
            },
        ],
        "missing_chunk_ids": ["missing"],
    }
    assert result.pipeline_run_id == "pipeline-9"
    assert [chunk.chunk_id for chunk in result.chunks] == ["a", "c"]
    assert result.missing_chunk_ids == ["missing"]
    assert [chunk.in_stock for chunk in result.chunks] == [True, False]
    assert store.request["entity_rows"] == [
        {"chunk_id": "a"},
        {"chunk_id": "missing"},
        {"chunk_id": "c"},
    ]
    assert store.request["features"] == [
        f"rag_item_chunks_green:{name}" for name in CHUNK_FEATURES
    ]
    assert store.request["full_feature_names"] is False
    assert _column_value({"a": [1]}, "a", 0) == 1
    assert _column_value({}, "a", 0) is None
    assert _as_bool(True) is True
    assert _as_bool(" 1 ") is True
    assert _as_bool("false") is False
    assert _as_bool(1) is True
    assert _as_bool(0) is False
    with pytest.raises(ValueError, match="Invalid in_stock"):
        _as_bool("maybe")


class RecordingEncoder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts):
        values = list(texts)
        self.calls.append(values)
        return [[float(int(value))] for value in values]

    def token_count(self, text: str) -> int:
        return len(text)


def encode_with_deadline(
    encoder: BatchingTextEncoder, texts: list[str]
) -> list[list[float]]:
    outcome: dict[str, object] = {}

    def invoke() -> None:
        try:
            outcome["result"] = encoder.encode(texts)
        except BaseException as exc:
            outcome["error"] = exc

    caller = REAL_THREAD(target=invoke, daemon=True)
    caller.start()
    caller.join(timeout=0.5)
    assert not caller.is_alive(), "encode did not finish within 500 ms"
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome["result"]  # type: ignore[return-value]


def test_batching_encoder_configuration_and_direct_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegate = RecordingEncoder()
    for kwargs, message in (
        ({"max_batch_size": 0}, "max_batch_size"),
        ({"max_wait_seconds": -1}, "max_wait_seconds"),
    ):
        created = None
        try:
            created = BatchingTextEncoder(delegate, **kwargs)
        except ValueError as exc:
            assert message in str(exc)
        else:
            created.close()
            pytest.fail(f"{message} validation was removed")

    class FakeThread:
        def __init__(self, *, target, name, daemon) -> None:
            self.target = target
            self.name = name
            self.daemon = daemon
            self.started = 0
            self.alive = True

        def start(self) -> None:
            self.started += 1

        def is_alive(self) -> bool:
            return self.alive

        def join(self, timeout) -> None:
            assert timeout == 5
            self.alive = False

    monkeypatch.setattr(batching_module.threading, "Thread", FakeThread)
    encoder = BatchingTextEncoder(delegate)
    assert encoder.delegate is delegate
    assert encoder.max_batch_size == 32
    assert encoder.max_wait_seconds == 0.01
    assert encoder._worker.target == encoder._run
    assert encoder._worker.name == "rag-query-encoder-batcher"
    assert encoder._worker.daemon is True
    assert encoder._worker.started == 1
    assert encoder.token_count("abc") == 3
    assert encode_with_deadline(encoder, []) == []
    assert encode_with_deadline(encoder, ["1"] * 33) == [[1.0]] * 33

    requests = Mock()
    encoder._requests = requests
    encoder.close()
    requests.put.assert_called_once_with(encoder._STOP)
    assert encoder._worker.alive is False
    encoder.close()
    assert requests.put.call_count == 1


def run_batch_worker(encoder: BatchingTextEncoder) -> None:
    worker = REAL_THREAD(target=encoder._run, daemon=True)
    worker.start()
    worker.join(timeout=0.5)
    assert not worker.is_alive(), "batch worker did not terminate after STOP"


def test_batching_worker_splits_results_and_propagates_failure() -> None:
    delegate = RecordingEncoder()
    encoder = BatchingTextEncoder.__new__(BatchingTextEncoder)
    encoder.delegate = delegate
    encoder.max_batch_size = 4
    encoder.max_wait_seconds = 0.05
    encoder._requests = queue.Queue()
    first = _EncodeRequest(("1", "2"))
    second = _EncodeRequest(("3",))
    encoder._requests.put(first)
    encoder._requests.put(second)
    encoder._requests.put(encoder._STOP)
    run_batch_worker(encoder)
    assert delegate.calls == [["1", "2", "3"]]
    assert first.result == [[1.0], [2.0]]
    assert second.result == [[3.0]]
    assert first.completed.is_set() and second.completed.is_set()

    class FailingEncoder(RecordingEncoder):
        def encode(self, texts):
            raise RuntimeError("encoder failed")

    encoder.delegate = FailingEncoder()
    encoder._requests = queue.Queue()
    failed = _EncodeRequest(("1",))
    encoder._requests.put(failed)
    encoder._requests.put(encoder._STOP)
    run_batch_worker(encoder)
    assert isinstance(failed.error, RuntimeError)
    assert str(failed.error) == "encoder failed"
    assert failed.completed.is_set()


def test_milvus_adapter_initialization_state() -> None:
    client = object()
    adapter = MilvusCandidateSearch(client, project="project", timeout_seconds=2.5)
    assert adapter.client is client
    assert adapter.project == "project"
    assert adapter.timeout_seconds == 2.5
    assert adapter._loaded == set()
    assert isinstance(adapter._load_lock, type(threading.Lock()))
