"""Blue/green Milvus publication state machine for RAG item embeddings.

Incremental updates are allowed only when stable entity keys cannot leave stale
rows. Deletions, chunk shrink, and embedding/chunker contract changes route to a
full inactive-slot reconciliation. Promotion is a final ETag-guarded pointer
write after exact ID/count/vector validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

from rag_data.artifact_storage import RagArtifactStore
from rag_data.feast_publisher import COLLECTION_BY_SLOT, FEATURE_VIEW_BY_SLOT
from rag_data.pipeline_contracts import (
    ActiveIndexPointer,
    ArtifactManifest,
    EmbeddedItemChunk,
    IndexManifest,
)


Mode = Literal["incremental", "reconcile"]
Slot = Literal["blue", "green"]


class Publisher(Protocol):
    """Boundary implemented by the Feast/Milvus adapter and unit-test fakes."""

    def reset_slot(self, slot: str) -> None:
        """Drop and recreate an inactive slot for full reconciliation."""

        ...

    def upsert(self, slot: str, records: list[EmbeddedItemChunk]) -> int:
        """Write records through Feast native online-store materialization."""

        ...

    def collection_count(self, slot: str) -> int:
        """Return the physical Milvus entity count for validation."""

        ...

    def collection_ids(self, slot: str, *, limit: int = 16_384) -> set[str]:
        """Return decoded Feast entity keys from the physical collection."""

        ...

    def smoke_search(self, slot: str, query_vector: list[float]) -> bool:
        """Run a minimal cosine retrieval probe against a candidate slot."""

        ...


@dataclass(frozen=True)
class PublishDecision:
    """Resolved publication mode, slot, and changed records."""

    mode: Mode
    target_slot: Slot
    records: list[EmbeddedItemChunk]
    reason: str


def inactive_slot(active_slot: str | None) -> Slot:
    """Select the slot that is safe for destructive reconciliation."""

    return "green" if active_slot == "blue" else "blue"


def decide_publish(
    *,
    requested_mode: Mode,
    current_manifest: ArtifactManifest,
    current_records: list[EmbeddedItemChunk],
    previous_manifest: ArtifactManifest | None,
    previous_records: list[EmbeddedItemChunk],
    active_slot: str | None,
) -> PublishDecision:
    """Route a candidate to safe native upsert or full reconciliation."""

    target: Slot = (
        active_slot if requested_mode == "incremental" and active_slot else inactive_slot(active_slot)
    )  # type: ignore[assignment]
    if requested_mode == "reconcile" or previous_manifest is None or active_slot is None:
        return PublishDecision("reconcile", inactive_slot(active_slot), current_records, "explicit_or_initial_reconcile")

    contract_changed = any(
        (
            current_manifest.chunker_version != previous_manifest.chunker_version,
            current_manifest.embedding_model != previous_manifest.embedding_model,
            current_manifest.embedding_revision != previous_manifest.embedding_revision,
            current_manifest.embedding_dimension != previous_manifest.embedding_dimension,
        )
    )
    old_items = set(previous_manifest.content_hashes)
    new_items = set(current_manifest.content_hashes)
    changed_items = {
        int(item_id)
        for item_id, value in current_manifest.content_hashes.items()
        if previous_manifest.content_hashes.get(item_id) != value
    }
    old_ids_by_item = {
        item_id: {record.chunk_id for record in previous_records if record.item_id == item_id}
        for item_id in changed_items
    }
    new_ids_by_item = {
        item_id: {record.chunk_id for record in current_records if record.item_id == item_id}
        for item_id in changed_items
    }
    chunk_shrank = any(
        not old_ids_by_item[item_id].issubset(new_ids_by_item[item_id])
        for item_id in changed_items
    )
    # Feast native writes upsert present entities but cannot remove stale ones.
    # Any condition capable of orphaning a chunk changes this state transition.
    if contract_changed or old_items - new_items or chunk_shrank:
        reason = "contract_change" if contract_changed else "deleted_item_or_chunk_shrink"
        return PublishDecision("reconcile", inactive_slot(active_slot), current_records, reason)
    changed_records = [
        record
        for record in current_records
        if record.item_id in changed_items or str(record.item_id) not in old_items
    ]
    return PublishDecision("incremental", target, changed_records, "safe_native_upsert")


def publish_index(
    *,
    store: RagArtifactStore,
    publisher: Publisher,
    run_id: str,
    requested_mode: Mode,
) -> IndexManifest:
    """Write a candidate or incremental update without changing active pointer."""

    current_manifest, current_records = store.load_embeddings(run_id)
    active = store.load_active_pointer()
    previous_manifest: ArtifactManifest | None = None
    previous_records: list[EmbeddedItemChunk] = []
    if active:
        previous_manifest, previous_records = store.load_embeddings(
            active.pointer.pipeline_run_id
        )
    decision = decide_publish(
        requested_mode=requested_mode,
        current_manifest=current_manifest,
        current_records=current_records,
        previous_manifest=previous_manifest,
        previous_records=previous_records,
        active_slot=active.pointer.active_slot if active else None,
    )
    if decision.mode == "reconcile":
        # Slot selection happens before this destructive operation; active slot is
        # never dropped, which preserves immediate pointer rollback capability.
        publisher.reset_slot(decision.target_slot)
    publisher.upsert(decision.target_slot, decision.records)
    manifest = IndexManifest(
        pipeline_run_id=run_id,
        slot=decision.target_slot,
        feature_view=FEATURE_VIEW_BY_SLOT[decision.target_slot],
        collection_name=COLLECTION_BY_SLOT[decision.target_slot],
        status="candidate",
        vector_count=publisher.collection_count(decision.target_slot),
        unique_item_count=current_manifest.unique_item_count,
    )
    store.write_index_manifest(run_id, manifest)
    return manifest


def validate_and_promote_index(
    *,
    store: RagArtifactStore,
    publisher: Publisher,
    run_id: str,
    promote: bool,
    expected_item_count: int = 0,
) -> IndexManifest:
    """Validate exact candidate IDs/counts and optionally CAS-promote its slot."""

    manifest, records = store.load_embeddings(run_id)
    publication = store.load_index_manifest(run_id)
    active = store.load_active_pointer()
    candidate_slot: Slot = publication.slot
    expected_ids = {record.chunk_id for record in records}
    actual_ids = publisher.collection_ids(candidate_slot)
    actual_count = publisher.collection_count(candidate_slot)
    smoke_passed = bool(records) and publisher.smoke_search(
        candidate_slot, records[0].embedding
    )
    count_mismatch = bool(expected_item_count) and manifest.unique_item_count != expected_item_count
    if actual_count != len(records) or actual_ids != expected_ids or not smoke_passed or count_mismatch:
        failed = IndexManifest(
            pipeline_run_id=run_id,
            slot=candidate_slot,
            feature_view=FEATURE_VIEW_BY_SLOT[candidate_slot],
            collection_name=COLLECTION_BY_SLOT[candidate_slot],
            status="failed",
            vector_count=actual_count,
            unique_item_count=manifest.unique_item_count,
            retrieval_smoke_passed=False,
            validated_at=datetime.now(timezone.utc),
        )
        store.write_index_manifest(run_id, failed)
        raise ValueError("Candidate Milvus IDs/count do not match gold manifest")
    validated = IndexManifest(
        pipeline_run_id=run_id,
        slot=candidate_slot,
        feature_view=FEATURE_VIEW_BY_SLOT[candidate_slot],
        collection_name=COLLECTION_BY_SLOT[candidate_slot],
        status="published" if promote else "validated",
        vector_count=actual_count,
        unique_item_count=manifest.unique_item_count,
        retrieval_smoke_passed=smoke_passed,
        validated_at=datetime.now(timezone.utc),
    )
    if promote:
        pointer = ActiveIndexPointer(
            active_slot=candidate_slot,
            feature_view=FEATURE_VIEW_BY_SLOT[candidate_slot],
            pipeline_run_id=run_id,
            source_run_id=manifest.source_run_id,
            chunker_version=manifest.chunker_version,
            embedding_model=manifest.embedding_model,
            embedding_revision=manifest.embedding_revision,
            previous_slot=(
                active.pointer.previous_slot
                if active and active.pointer.active_slot == candidate_slot
                else active.pointer.active_slot if active else None
            ),
            previous_pipeline_run_id=(
                active.pointer.previous_pipeline_run_id
                if active and active.pointer.active_slot == candidate_slot
                else active.pointer.pipeline_run_id if active else None
            ),
        )
        store.compare_and_swap_pointer(
            pointer, expected_etag=active.etag if active else None
        )
    store.write_index_manifest(run_id, validated)
    return validated


def rollback_active_pointer(*, store: RagArtifactStore) -> ActiveIndexPointer:
    """Atomically restore the previous validated slot after post-promotion failure."""

    active = store.load_active_pointer()
    if (
        active is None
        or active.pointer.previous_slot is None
        or active.pointer.previous_pipeline_run_id is None
    ):
        raise ValueError("Active pointer has no rollback target")
    previous_manifest, _ = store.load_embeddings(
        active.pointer.previous_pipeline_run_id
    )
    restored = ActiveIndexPointer(
        active_slot=active.pointer.previous_slot,
        feature_view=FEATURE_VIEW_BY_SLOT[active.pointer.previous_slot],
        pipeline_run_id=active.pointer.previous_pipeline_run_id,
        source_run_id=previous_manifest.source_run_id,
        chunker_version=previous_manifest.chunker_version,
        embedding_model=previous_manifest.embedding_model,
        embedding_revision=previous_manifest.embedding_revision,
        previous_slot=active.pointer.active_slot,
        previous_pipeline_run_id=active.pointer.pipeline_run_id,
    )
    # Rollback is the inverse state transition and uses the same ETag CAS; it
    # cannot clobber a newer emergency promotion performed by another operator.
    store.compare_and_swap_pointer(restored, expected_etag=active.etag)
    return restored
