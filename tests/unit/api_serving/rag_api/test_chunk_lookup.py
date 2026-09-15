from __future__ import annotations

from types import SimpleNamespace

from recsys_rag_api.chunk_lookup import CHUNK_FEATURES, ChunkLookupService


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


class Pointers:
    def get(self):
        return SimpleNamespace(
            feature_view="rag_item_chunks_green", pipeline_run_id="pipeline-7"
        )


def test_lookup_uses_pointer_selected_view_and_preserves_request_order():
    store = Store()
    service = ChunkLookupService(feature_store=store, pointers=Pointers())

    result = service.get_many(["a", "missing", "c"])

    assert result.pipeline_run_id == "pipeline-7"
    assert [chunk.model_dump() for chunk in result.chunks] == [
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
    ]
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
    assert all("embedding" not in feature for feature in store.request["features"])
