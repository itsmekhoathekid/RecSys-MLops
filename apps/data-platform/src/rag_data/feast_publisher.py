"""Feast-native item chunk upsert and Milvus collection validation.

Feast owns the physical schema and all writes use ``write_to_online_store``.
The direct Milvus client is deliberately limited to destructive reconciliation
of the inactive slot and read-only validation, operations Feast does not expose
at row level.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from rag_data.pipeline_contracts import EmbeddedItemChunk


FEATURE_VIEW_BY_SLOT = {
    "blue": "rag_item_chunks_blue",
    "green": "rag_item_chunks_green",
}
COLLECTION_BY_SLOT = {
    slot: f"recsys_rag_{feature_view}"
    for slot, feature_view in FEATURE_VIEW_BY_SLOT.items()
}


class FeastMilvusPublisher:
    """Publish records through Feast and inspect the corresponding Milvus slot."""

    def __init__(
        self,
        *,
        repo_path: str | Path,
        milvus_uri: str,
        milvus_token: str,
    ) -> None:
        from feast import FeatureStore
        from pymilvus import MilvusClient

        self.store = FeatureStore(repo_path=str(repo_path))
        self.milvus = MilvusClient(uri=milvus_uri, token=milvus_token)

    @staticmethod
    def feature_view(slot: str) -> str:
        """Resolve a blue/green slot to its registered Feast feature view."""

        try:
            return FEATURE_VIEW_BY_SLOT[slot]
        except KeyError as exc:
            raise ValueError(f"Unknown index slot: {slot!r}") from exc

    def reset_slot(self, slot: str) -> None:
        """Drop the inactive collection before a full reconciliation write.

        This operation is destructive, so callers must prove the slot is not the
        active pointer. The next Feast write recreates its schema and FLAT index.
        """

        collection = COLLECTION_BY_SLOT[slot]
        if self.milvus.has_collection(collection):
            self.milvus.drop_collection(collection_name=collection)

    def upsert(self, slot: str, records: Iterable[EmbeddedItemChunk]) -> int:
        """Native-upsert records into one Feast feature view.

        Feast/Milvus upserts by stable ``chunk_id``. Feast has no stale-row delete
        API for this path; delete or chunk shrink must use inactive-slot reconcile.
        """

        rows = []
        for record in records:
            value = record.model_dump(mode="python")
            rows.append(
                {
                    key: value[key]
                    for key in (
                        "chunk_id",
                        "embedding",
                        "item_id",
                        "chunk_type",
                        "source_key",
                        "text",
                        "brand",
                        "category_l1",
                        "category_l2",
                        "category_l3",
                        "current_price",
                        "in_stock",
                        "average_rating",
                        "content_hash",
                        "item_content_hash",
                        "source_run_id",
                        "event_timestamp",
                    )
                }
            )
        if not rows:
            return 0
        frame = pd.DataFrame.from_records(rows)
        self.store.write_to_online_store(
            feature_view_name=self.feature_view(slot), df=frame
        )
        return len(rows)

    def collection_count(self, slot: str) -> int:
        """Return flushed entity count from the physical Feast collection."""

        collection = COLLECTION_BY_SLOT[slot]
        self.milvus.flush(collection_name=collection)
        stats: dict[str, Any] = self.milvus.get_collection_stats(
            collection_name=collection
        )
        return int(stats.get("row_count", 0))

    def collection_ids(self, slot: str, *, limit: int = 16_384) -> set[str]:
        """Read entity chunk IDs for exact candidate validation."""

        from feast.infra.key_encoding_utils import deserialize_entity_key

        collection = COLLECTION_BY_SLOT[slot]
        rows = self.milvus.query(
            collection_name=collection,
            # Feast persists feature payloads in a flexible JSON field, so scalar
            # feature types are unsuitable for a Milvus filter expression here.
            # The encoded string primary key is always present and filterable.
            filter='chunk_id_pk != ""',
            output_fields=["chunk_id_pk"],
            limit=limit,
        )
        output: set[str] = set()
        for row in rows:
            entity_key = deserialize_entity_key(
                bytes.fromhex(str(row["chunk_id_pk"])),
                entity_key_serialization_version=3,
            )
            output.add(entity_key.entity_values[0].string_val)
        return output

    def smoke_search(self, slot: str, query_vector: list[float]) -> bool:
        """Run one COSINE search against a loaded candidate collection."""

        collection = COLLECTION_BY_SLOT[slot]
        self.milvus.load_collection(collection_name=collection)
        result = self.milvus.search(
            collection_name=collection,
            data=[query_vector],
            anns_field="embedding",
            search_params={"metric_type": "COSINE", "params": {}},
            limit=1,
            output_fields=["item_id"],
        )
        return bool(result and result[0])
