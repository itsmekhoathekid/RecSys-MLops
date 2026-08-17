"""Idempotent orchestration for item chunking and offline embedding artifacts.

The runners validate complete upstream manifests, checkpoint run-scoped objects,
and mark outputs complete only after every record passes its strict contract.
They do not publish a Milvus index; publication is a separate auditable command.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rag_data.artifact_storage import RagArtifactStore
from rag_data.embedding import TextEncoder, encode_with_fallback
from rag_data.pipeline_contracts import ArtifactManifest, EmbeddedItemChunk
from rag_data.semantic_chunker import ChunkerConfig, SemanticChunker


def chunk_canonical_items(
    *,
    store: RagArtifactStore,
    encoder: TextEncoder,
    source_run_id: str,
    run_id: str,
    config: ChunkerConfig,
    embedding_model: str,
    embedding_revision: str,
    model_checksum: str,
    force: bool = False,
    checkpoint_every: int = 10,
    item_limit: int = 0,
) -> ArtifactManifest:
    """Build and checkpoint the complete silver chunk artifact.

    Repeating a completed compatible run is a no-op. ``force`` permits replacing
    only the named run prefix. Any invalid item fails the command before complete
    status is written.
    """

    existing = store.load_manifest(run_id, zone="silver")
    if existing and existing.status == "complete" and not force:
        return existing
    if existing and not force and (
        existing.source_run_id != source_run_id
        or existing.chunker_version != config.version
        or existing.embedding_model != embedding_model
        or existing.embedding_revision != embedding_revision
        or existing.model_checksum != model_checksum
    ):
        raise ValueError("Existing silver checkpoint uses an incompatible contract; pass force")
    if checkpoint_every < 1:
        raise ValueError("checkpoint_every must be >= 1")
    if item_limit < 0:
        raise ValueError("item_limit must be >= 0")
    _, items = store.load_canonical_items(source_run_id)
    # CD smoke runs use an isolated run ID and the first deterministic IDs. The
    # production run remains complete and reconciliation resets the same inactive
    # slot before writing the full dataset, so smoke rows can never be promoted.
    if item_limit:
        items = items[:item_limit]
    manifest = ArtifactManifest(
        dataset_type="rag_item_chunks",
        run_id=run_id,
        source_run_id=source_run_id,
        status="running",
        chunker_version=config.version,
        embedding_model=embedding_model,
        embedding_revision=embedding_revision,
        model_checksum=model_checksum,
    )
    chunks = [] if force else store.load_partial_chunks(run_id)
    completed_items = {chunk.item_id for chunk in chunks}
    store.write_chunks(run_id, chunks, manifest)
    chunker = SemanticChunker(encoder, config)
    processed_since_checkpoint = 0
    try:
        for item in items:
            if item.item_id in completed_items:
                continue
            chunks.extend(chunker.chunk_item(item, source_run_id=source_run_id))
            processed_since_checkpoint += 1
            if processed_since_checkpoint >= checkpoint_every:
                running = manifest.refreshed(
                    record_count=len(chunks),
                    unique_item_count=len({chunk.item_id for chunk in chunks}),
                )
                store.write_chunks(run_id, chunks, running)
                processed_since_checkpoint = 0
    except Exception as exc:
        partial = manifest.refreshed(
            status="partial",
            record_count=len(chunks),
            unique_item_count=len({chunk.item_id for chunk in chunks}),
            failed_count=1,
        )
        store.write_chunks(
            run_id,
            chunks,
            partial,
            [{"error_type": type(exc).__name__, "message": str(exc)}],
        )
        raise
    if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
        raise ValueError("Duplicate chunk IDs generated across checkpoints")
    hashes = {
        str(item_id): next(
            chunk.item_content_hash for chunk in chunks if chunk.item_id == item_id
        )
        for item_id in sorted({chunk.item_id for chunk in chunks})
    }
    complete = manifest.refreshed(
        status="complete",
        record_count=len(chunks),
        unique_item_count=len(hashes),
        content_hashes=hashes,
    )
    store.write_chunks(run_id, chunks, complete)
    return complete


def embed_item_chunks(
    *,
    store: RagArtifactStore,
    encoder: TextEncoder,
    run_id: str,
    force: bool = False,
    checkpoint_every: int = 10,
) -> ArtifactManifest:
    """Embed a complete silver run and write its normalized gold artifact.

    The same ``run_id`` is used for silver and gold. The function is idempotent
    after completion and performs no Feast/Milvus writes.
    """

    existing = store.load_manifest(run_id, zone="gold")
    if existing and existing.status == "complete" and not force:
        return existing
    if checkpoint_every < 1:
        raise ValueError("checkpoint_every must be >= 1")
    silver, chunks = store.load_chunks(run_id)
    if existing and not force and (
        existing.source_run_id != silver.source_run_id
        or existing.chunker_version != silver.chunker_version
        or existing.embedding_revision != silver.embedding_revision
        or existing.model_checksum != silver.model_checksum
    ):
        raise ValueError("Existing gold checkpoint uses an incompatible contract; pass force")
    manifest = ArtifactManifest(
        dataset_type="rag_item_embeddings",
        run_id=run_id,
        source_run_id=silver.source_run_id,
        status="running",
        chunker_version=silver.chunker_version,
        embedding_model=silver.embedding_model,
        embedding_revision=silver.embedding_revision,
        model_checksum=silver.model_checksum,
        content_hashes=silver.content_hashes,
    )
    records = [] if force else store.load_partial_embeddings(run_id)
    completed_ids = {record.chunk_id for record in records}
    store.write_embeddings(run_id, records, manifest)
    pending = [chunk for chunk in chunks if chunk.chunk_id not in completed_ids]
    try:
        for start in range(0, len(pending), checkpoint_every):
            batch = pending[start : start + checkpoint_every]
            vectors = encode_with_fallback(
                encoder, [chunk.embedding_text for chunk in batch]
            )
            records.extend(
                EmbeddedItemChunk(**chunk.model_dump(mode="python"), embedding=vector)
                for chunk, vector in zip(batch, vectors, strict=True)
            )
            running = manifest.refreshed(
                record_count=len(records),
                unique_item_count=len({record.item_id for record in records}),
            )
            store.write_embeddings(run_id, records, running)
    except Exception as exc:
        partial = manifest.refreshed(
            status="partial",
            record_count=len(records),
            unique_item_count=len({record.item_id for record in records}),
            failed_count=1,
        )
        store.write_embeddings(
            run_id,
            records,
            partial,
            [{"error_type": type(exc).__name__, "message": str(exc)}],
        )
        raise
    if len({record.chunk_id for record in records}) != len(records):
        raise ValueError("Duplicate embedding chunk IDs across checkpoints")
    complete = manifest.refreshed(
        status="complete",
        record_count=len(records),
        unique_item_count=len({record.item_id for record in records}),
    )
    store.write_embeddings(run_id, records, complete)
    return complete
