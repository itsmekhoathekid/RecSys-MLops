from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from recsys_rag_api.contracts import CandidateChunk, RetrievalFilters, RetrievalRequest
from recsys_rag_api.pointer import ActivePointerManager, EmbeddingContract
from recsys_rag_api.retrieval import (
    FeastCandidateSearch,
    MilvusCandidateSearch,
    RetrievalService,
    _matches,
)


REVISION = "03415a4be176a1620747c692ed433219fabc3def"


class Encoder:
    def encode(self, texts):
        assert texts[0].startswith("query: ")
        return [[1.0] + [0.0] * 383]

    def token_count(self, text):
        return len(text.split())


class Search:
    def __init__(self, rows):
        self.rows = rows
        self.pools = []

    def search(self, *, feature_view, query_vector, top_k):
        self.pools.append(top_k)
        return self.rows[:top_k]


def pointer_bytes(run="run-1", revision=REVISION):
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


def candidate(item_id, chunk_id, score, rating=4.5, stock=True):
    return CandidateChunk(
        chunk_id=chunk_id,
        item_id=item_id,
        chunk_type="review",
        source_key="review",
        text="evidence",
        brand="Sony",
        category_l1="Điện tử",
        category_l2="Thiết bị âm thanh",
        category_l3="Tai nghe",
        current_price=20.99,
        in_stock=stock,
        average_rating=rating,
        score=score,
    )


def manager(loader=lambda: pointer_bytes()):
    return ActivePointerManager(
        loader=loader,
        supported_contracts=[
            EmbeddingContract("intfloat/multilingual-e5-small", REVISION, 384)
        ],
        reload_seconds=0,
    )


def test_filter_group_evidence_cap_and_tie_break():
    rows = [
        candidate(2, "2:a", 0.9, rating=4.6),
        candidate(1, "1:a", 0.9, rating=4.8),
        candidate(1, "1:b", 0.8, rating=4.8),
        candidate(1, "1:c", 0.7, rating=4.8),
        candidate(3, "3:a", 0.99, stock=False),
    ]
    service = RetrievalService(
        encoder=Encoder(), search=Search(rows), pointers=manager()
    )
    response = service.retrieve(
        RetrievalRequest.model_validate(
            {
                "query": "tai nghe văn phòng",
                "top_k_items": 2,
                "filters": {"in_stock": True},
            }
        )
    )
    assert [item.item_id for item in response.items] == [1, 2]
    assert len(response.items[0].evidence) == 2
    assert response.pipeline_run_id == "run-1"


def test_pointer_keeps_last_known_good_after_bad_reload():
    payloads = iter([pointer_bytes(), pointer_bytes(run="bad", revision="unsupported")])
    pointers = manager(lambda: next(payloads))
    assert pointers.get().pipeline_run_id == "run-1"
    assert pointers.get().pipeline_run_id == "run-1"


def test_cold_pointer_and_request_validation_fail_closed():
    pointers = manager(lambda: b"not-json")
    with pytest.raises(RuntimeError, match="No valid active"):
        pointers.get()
    with pytest.raises(ValidationError):
        RetrievalRequest.model_validate({"query": "   "})
    with pytest.raises(ValidationError):
        RetrievalFilters.model_validate({"brands": [""]})


@pytest.mark.parametrize(
    ("filters", "matches"),
    [
        ({"brands": ["Bose"]}, False),
        ({"category_prefix": ["Điện tử", "Khác"]}, False),
        ({"min_current_price": 30}, False),
        ({"max_current_price": 10}, False),
        ({"in_stock": False}, False),
        ({"chunk_types": ["qna"]}, False),
        ({"brands": ["sony"], "category_prefix": ["Điện tử"]}, True),
    ],
)
def test_all_scalar_filter_branches(filters, matches):
    assert _matches(candidate(1, "1:a", 0.9), RetrievalFilters(**filters)) is matches


def test_candidate_pool_refills_until_enough_unique_items():
    rows = [candidate(1, f"1:{index}", 0.9) for index in range(120)] + [
        candidate(2, "2:a", 0.8)
    ]
    search = Search(rows)
    response = RetrievalService(
        encoder=Encoder(), search=search, pointers=manager()
    ).retrieve(RetrievalRequest(query="tai nghe", top_k_items=2))
    assert [item.item_id for item in response.items] == [1, 2]
    assert search.pools == [100, 200]


class FeastResponse:
    def __init__(self, values):
        self.values = values

    def to_dict(self):
        return self.values


class FeatureStore:
    def __init__(self, values):
        self.values = values
        self.kwargs = None

    def retrieve_online_documents_v2(self, **kwargs):
        self.kwargs = kwargs
        return FeastResponse(self.values)


def test_feast_adapter_coerces_column_response_and_empty_result():
    values = {
        "chunk_id": ["1:a"],
        "item_id": ["1"],
        "chunk_type": ["review"],
        "source_key": ["r"],
        "text": ["text"],
        "brand": ["Sony"],
        "category_l1": ["Điện tử"],
        "current_price": ["20.99"],
        "in_stock": ["true"],
        "average_rating": ["4.7"],
        "distance": ["0.91"],
    }
    store = FeatureStore(values)
    query_vector = [1.0] * 384
    rows = FeastCandidateSearch(store).search(
        feature_view="rag_item_chunks_blue", query_vector=query_vector, top_k=5
    )
    assert rows == [
        CandidateChunk(
            chunk_id="1:a",
            item_id=1,
            chunk_type="review",
            source_key="r",
            text="text",
            brand="Sony",
            category_l1="Điện tử",
            category_l2="",
            category_l3="",
            current_price=20.99,
            in_stock=True,
            average_rating=4.7,
            score=0.91,
        )
    ]
    assert store.kwargs == {
        "features": [
            f"rag_item_chunks_blue:{field}" for field in FeastCandidateSearch.FIELDS
        ],
        "query": query_vector,
        "top_k": 5,
        "distance_metric": "COSINE",
    }
    assert (
        FeastCandidateSearch(FeatureStore({})).search(
            feature_view="rag_item_chunks_blue", query_vector=query_vector, top_k=5
        )
        == []
    )


def test_feast_adapter_supports_qualified_columns_and_multiple_rows():
    view = "rag_item_chunks_green"
    values = {
        f"{view}:chunk_id": ["1:a", "2:b"],
        f"{view}:item_id": ["1", "2"],
        f"{view}:chunk_type": ["review", "qna"],
        f"{view}:source_key": ["r1", "q2"],
        f"{view}:text": ["first", "second"],
        f"{view}:brand": ["Sony", "Bose"],
        f"{view}:category_l1": ["Audio", "Audio"],
        f"{view}:category_l2": ["Headphones", "Speakers"],
        f"{view}:category_l3": ["Wireless", "Portable"],
        f"{view}:current_price": ["20.5", "30.5"],
        f"{view}:in_stock": ["1", "false"],
        f"{view}:average_rating": ["4.1", "4.2"],
        f"{view}:distance": ["0.91", "0.81"],
    }

    rows = FeastCandidateSearch(FeatureStore(values)).search(
        feature_view=view, query_vector=[0.25, -0.5], top_k=2
    )

    assert [row.model_dump() for row in rows] == [
        {
            "chunk_id": "1:a",
            "item_id": 1,
            "chunk_type": "review",
            "source_key": "r1",
            "text": "first",
            "brand": "Sony",
            "category_l1": "Audio",
            "category_l2": "Headphones",
            "category_l3": "Wireless",
            "current_price": 20.5,
            "in_stock": True,
            "average_rating": 4.1,
            "score": 0.91,
        },
        {
            "chunk_id": "2:b",
            "item_id": 2,
            "chunk_type": "qna",
            "source_key": "q2",
            "text": "second",
            "brand": "Bose",
            "category_l1": "Audio",
            "category_l2": "Speakers",
            "category_l3": "Portable",
            "current_price": 30.5,
            "in_stock": False,
            "average_rating": 4.2,
            "score": 0.81,
        },
    ]


class MilvusClient:
    def __init__(self, results=None):
        self.loaded = []
        self.calls = []
        self.results = results

    def load_collection(self, *, collection_name, timeout):
        self.loaded.append((collection_name, timeout))

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return (
            self.results
            if self.results is not None
            else [
                [
                    {
                        "distance": 0.92,
                        "entity": {
                            "chunk_id": "1:a",
                            "item_id": "1",
                            "chunk_type": "review",
                            "source_key": "r",
                            "text": "text",
                            "brand": "Sony",
                            "category_l1": "Điện tử",
                            "current_price": "20.99",
                            "in_stock": "true",
                            "average_rating": "4.7",
                        },
                    }
                ]
            ]
        )


def test_milvus_adapter_reuses_loaded_collection_and_omits_embedding_payload():
    client = MilvusClient()
    adapter = MilvusCandidateSearch(client, project="recsys_rag", timeout_seconds=5.0)
    first_vector = [1.0] * 384
    first = adapter.search(
        feature_view="rag_item_chunks_blue", query_vector=first_vector, top_k=200
    )
    second_vector = [2.0] * 384
    adapter.search(
        feature_view="rag_item_chunks_blue", query_vector=second_vector, top_k=100
    )
    assert [row.model_dump() for row in first] == [
        {
            "chunk_id": "1:a",
            "item_id": 1,
            "chunk_type": "review",
            "source_key": "r",
            "text": "text",
            "brand": "Sony",
            "category_l1": "Điện tử",
            "category_l2": "",
            "category_l3": "",
            "current_price": 20.99,
            "in_stock": True,
            "average_rating": 4.7,
            "score": 0.92,
        }
    ]
    assert client.loaded == [("recsys_rag_rag_item_chunks_blue", 5.0)]
    assert client.calls == [
        {
            "collection_name": "recsys_rag_rag_item_chunks_blue",
            "data": [first_vector],
            "anns_field": "embedding",
            "search_params": {"metric_type": "COSINE", "params": {}},
            "limit": 200,
            "output_fields": list(MilvusCandidateSearch.OUTPUT_FIELDS),
            "timeout": 5.0,
        },
        {
            "collection_name": "recsys_rag_rag_item_chunks_blue",
            "data": [second_vector],
            "anns_field": "embedding",
            "search_params": {"metric_type": "COSINE", "params": {}},
            "limit": 100,
            "output_fields": list(MilvusCandidateSearch.OUTPUT_FIELDS),
            "timeout": 5.0,
        },
    ]


def test_milvus_adapter_handles_empty_results_and_flat_hit_fallbacks():
    empty = MilvusClient(results=[])
    adapter = MilvusCandidateSearch(empty, project="catalog", timeout_seconds=2.5)
    assert adapter.search(feature_view="green", query_vector=[0.5], top_k=3) == []
    assert empty.loaded == [("catalog_green", 2.5)]
    assert empty.calls == [
        {
            "collection_name": "catalog_green",
            "data": [[0.5]],
            "anns_field": "embedding",
            "search_params": {"metric_type": "COSINE", "params": {}},
            "limit": 3,
            "output_fields": list(MilvusCandidateSearch.OUTPUT_FIELDS),
            "timeout": 2.5,
        }
    ]

    flat_hit = {
        "chunk_id": "9:q",
        "item_id": "9",
        "chunk_type": "qna",
        "source_key": "questions/9",
        "text": "flat",
        "brand": "Acme",
        "category_l1": "Audio",
        "category_l2": "Portable",
        "category_l3": "Mini",
        "current_price": "9.5",
        "in_stock": "1",
        "average_rating": "4.9",
        "score": "0.75",
    }
    assert MilvusCandidateSearch._candidate(flat_hit).model_dump() == {
        "chunk_id": "9:q",
        "item_id": 9,
        "chunk_type": "qna",
        "source_key": "questions/9",
        "text": "flat",
        "brand": "Acme",
        "category_l1": "Audio",
        "category_l2": "Portable",
        "category_l3": "Mini",
        "current_price": 9.5,
        "in_stock": True,
        "average_rating": 4.9,
        "score": 0.75,
    }


def test_milvus_candidate_prefers_entity_values_and_distance_attribute():
    class Hit(dict):
        distance = 0.88

    hit = Hit(
        distance=0.11,
        score=0.22,
        entity={
            "chunk_id": "4:r",
            "item_id": "4",
            "chunk_type": "review",
            "source_key": "reviews/4",
            "text": "entity",
            "brand": "EntityBrand",
            "category_l1": "One",
            "category_l2": "Two",
            "category_l3": "Three",
            "current_price": "44.0",
            "in_stock": "false",
            "average_rating": "4.4",
        },
        chunk_id="wrong",
        item_id="99",
        brand="WrongBrand",
    )

    assert MilvusCandidateSearch._candidate(hit).model_dump() == {
        "chunk_id": "4:r",
        "item_id": 4,
        "chunk_type": "review",
        "source_key": "reviews/4",
        "text": "entity",
        "brand": "EntityBrand",
        "category_l1": "One",
        "category_l2": "Two",
        "category_l3": "Three",
        "current_price": 44.0,
        "in_stock": False,
        "average_rating": 4.4,
        "score": 0.88,
    }
