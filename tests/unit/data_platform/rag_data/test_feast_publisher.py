from __future__ import annotations

from rag_data.feast_publisher import FeastMilvusPublisher


class _LoadAwareMilvus:
    def __init__(self) -> None:
        self.loaded = False

    def load_collection(self, *, collection_name: str) -> None:
        assert collection_name == "recsys_rag_rag_item_chunks_blue"
        self.loaded = True

    def query(self, **_kwargs):
        assert self.loaded, "candidate collection must be loaded before exact-ID query"
        return []


def test_collection_ids_loads_candidate_before_querying() -> None:
    publisher = FeastMilvusPublisher.__new__(FeastMilvusPublisher)
    publisher.milvus = _LoadAwareMilvus()

    assert publisher.collection_ids("blue") == set()
