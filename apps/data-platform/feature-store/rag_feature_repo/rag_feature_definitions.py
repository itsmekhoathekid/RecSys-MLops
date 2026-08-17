"""Feast definitions for the blue/green RAG item vector collections.

The two views intentionally have identical schemas and distinct names. Their
versions audit schema evolution only; dataset releases are selected by the
external active pointer rather than Feast feature-view versioning.
"""

from datetime import timedelta

from feast import Entity, FeatureView, Field, FileSource
from feast.types import Array, Bool, Float32, Float64, Int64, String, ValueType


chunk = Entity(
    name="chunk",
    join_keys=["chunk_id"],
    value_type=ValueType.STRING,
    description="Stable RAG chunk entity; content changes upsert the same key.",
)

# Direct writers use write_to_online_store(); this source is a registry contract
# and is never materialized. Gold Parquet remains the authoritative batch artifact.
rag_artifact_source = FileSource(
    name="rag_item_embeddings_artifact",
    path="/tmp/rag_item_embeddings.parquet",
    timestamp_field="event_timestamp",
)


def _rag_chunk_view(name: str) -> FeatureView:
    """Build one slot with the shared 384-D COSINE retrieval schema."""

    return FeatureView(
        name=name,
        entities=[chunk],
        ttl=timedelta(days=3650),
        online=True,
        source=rag_artifact_source,
        schema=[
            Field(
                name="embedding",
                dtype=Array(Float32),
                vector_index=True,
                vector_search_metric="COSINE",
            ),
            Field(name="item_id", dtype=Int64),
            Field(name="chunk_type", dtype=String),
            Field(name="source_key", dtype=String),
            Field(name="text", dtype=String),
            Field(name="brand", dtype=String),
            Field(name="category_l1", dtype=String),
            Field(name="category_l2", dtype=String),
            Field(name="category_l3", dtype=String),
            Field(name="current_price", dtype=Float64),
            Field(name="in_stock", dtype=Bool),
            Field(name="average_rating", dtype=Float64),
            Field(name="content_hash", dtype=String),
            Field(name="item_content_hash", dtype=String),
            Field(name="source_run_id", dtype=String),
        ],
        tags={"data_product": "RAG_ITEMS", "slot": name.rsplit("_", 1)[-1]},
    )


rag_item_chunks_blue = _rag_chunk_view("rag_item_chunks_blue")
rag_item_chunks_green = _rag_chunk_view("rag_item_chunks_green")
