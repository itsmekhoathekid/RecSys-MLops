"""Exact RAG chunk lookup through the active Feast online FeatureView."""

from __future__ import annotations

from typing import Protocol

from recsys_rag_api.contracts import ChunkBatchResponse, ChunkRecord
from recsys_rag_api.pointer import ActivePointerManager


CHUNK_FEATURES = (
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
    "source_run_id",
)


class OnlineFeatureStore(Protocol):
    """Small Feast boundary used by exact lookup and replaced in unit tests."""

    def get_online_features(
        self,
        *,
        features: list[str],
        entity_rows: list[dict[str, str]],
        full_feature_names: bool,
    ) -> object: ...


class PointerProvider(Protocol):
    """Active pointer boundary shared with semantic retrieval."""

    def get(self) -> object: ...


class ChunkLookupService:
    """Read exact chunk metadata from the pointer-selected online view."""

    def __init__(
        self,
        *,
        feature_store: OnlineFeatureStore,
        pointers: ActivePointerManager | PointerProvider,
    ) -> None:
        self.feature_store = feature_store
        self.pointers = pointers

    def get_many(self, chunk_ids: list[str]) -> ChunkBatchResponse:
        """Return records in request order and classify absent entity rows."""

        pointer = self.pointers.get()
        feature_view = str(getattr(pointer, "feature_view"))
        pipeline_run_id = str(getattr(pointer, "pipeline_run_id"))
        result = self.feature_store.get_online_features(
            features=[f"{feature_view}:{name}" for name in CHUNK_FEATURES],
            entity_rows=[{"chunk_id": chunk_id} for chunk_id in chunk_ids],
            full_feature_names=False,
        )
        columns = result.to_dict()
        records: list[ChunkRecord] = []
        missing: list[str] = []
        for index, chunk_id in enumerate(chunk_ids):
            item_id = _column_value(columns, "item_id", index)
            text = _column_value(columns, "text", index)
            if item_id is None or text is None:
                missing.append(chunk_id)
                continue
            records.append(
                ChunkRecord(
                    chunk_id=chunk_id,
                    item_id=int(item_id),
                    chunk_type=str(_column_value(columns, "chunk_type", index)),
                    source_key=str(_column_value(columns, "source_key", index)),
                    text=str(text),
                    brand=str(_column_value(columns, "brand", index) or ""),
                    category_l1=str(
                        _column_value(columns, "category_l1", index) or ""
                    ),
                    category_l2=str(
                        _column_value(columns, "category_l2", index) or ""
                    ),
                    category_l3=str(
                        _column_value(columns, "category_l3", index) or ""
                    ),
                    current_price=float(
                        _column_value(columns, "current_price", index) or 0.0
                    ),
                    in_stock=bool(_column_value(columns, "in_stock", index)),
                    average_rating=float(
                        _column_value(columns, "average_rating", index) or 0.0
                    ),
                    source_run_id=str(
                        _column_value(columns, "source_run_id", index) or ""
                    ),
                )
            )
        return ChunkBatchResponse(
            pipeline_run_id=pipeline_run_id,
            chunks=records,
            missing_chunk_ids=missing,
        )


def _column_value(columns: dict[str, list[object]], name: str, index: int) -> object:
    """Read one Feast result value while tolerating a short/missing column."""

    values = columns.get(name, [])
    return values[index] if index < len(values) else None
