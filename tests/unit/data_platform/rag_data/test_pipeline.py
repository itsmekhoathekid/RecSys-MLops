from __future__ import annotations

from rag_data.pipeline import chunk_canonical_items
from rag_data.semantic_chunker import ChunkerConfig, SemanticChunker
from test_semantic_chunker import FakeEncoder, item


class Store:
    def __init__(self, items, partial=None):
        self.items = items
        self.partial = partial or []
        self.writes = []

    def load_manifest(self, run_id, *, zone):
        return None

    def load_canonical_items(self, source_run_id):
        return object(), self.items

    def load_partial_chunks(self, run_id):
        return list(self.partial)

    def write_chunks(self, run_id, chunks, manifest, failures=None):
        self.writes.append((list(chunks), manifest, failures or []))


def test_chunk_pipeline_limits_cd_smoke_and_resumes_completed_items():
    encoder = FakeEncoder()
    first = item()
    second = item().model_copy(update={"item_id": 800001, "sku": "SKU-800001"})
    existing = SemanticChunker(encoder, ChunkerConfig()).chunk_item(
        first, source_run_id="source"
    )
    store = Store([first, second], partial=existing)
    result = chunk_canonical_items(
        store=store,
        encoder=encoder,
        source_run_id="source",
        run_id="smoke",
        config=ChunkerConfig(),
        embedding_model="intfloat/multilingual-e5-small",
        embedding_revision="revision",
        model_checksum="sha256:model",
        item_limit=2,
        checkpoint_every=1,
    )
    assert result.status == "complete"
    assert result.unique_item_count == 2
    assert {chunk.item_id for chunk in store.writes[-1][0]} == {800000, 800001}

    smoke_store = Store([first, second])
    smoke = chunk_canonical_items(
        store=smoke_store,
        encoder=encoder,
        source_run_id="source",
        run_id="smoke-3",
        config=ChunkerConfig(),
        embedding_model="intfloat/multilingual-e5-small",
        embedding_revision="revision",
        model_checksum="sha256:model",
        item_limit=1,
    )
    assert smoke.unique_item_count == 1
