from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rag_data.artifact_storage import VersionedPointer
from rag_data.index_lifecycle import (
    decide_publish,
    inactive_slot,
    publish_index,
    rollback_active_pointer,
    validate_and_promote_index,
)
from rag_data.pipeline_contracts import (
    ActiveIndexPointer,
    ArtifactManifest,
    EmbeddedItemChunk,
    IndexManifest,
)


def manifest(run: str, hashes: dict[str, str], revision: str = "rev-1"):
    return ArtifactManifest(
        dataset_type="rag_item_embeddings",
        run_id=run,
        source_run_id="source",
        status="complete",
        record_count=len(hashes),
        unique_item_count=len(hashes),
        chunker_version="semantic_chunker_v1",
        embedding_model="intfloat/multilingual-e5-small",
        embedding_revision=revision,
        model_checksum="sha256:model",
        content_hashes=hashes,
    )


def record(item_id: int, part: int = 0) -> EmbeddedItemChunk:
    vector = [0.0] * 384
    vector[0] = 1.0
    return EmbeddedItemChunk(
        chunk_id=f"{item_id}:review:r:{part}",
        item_id=item_id,
        chunk_type="review",
        source_key="r",
        chunk_index=part,
        text="text",
        embedding_text="passage: text",
        token_count=2,
        content_hash="sha256:" + "a" * 64,
        item_content_hash="sha256:" + "b" * 64,
        brand="Sony",
        current_price=20.99,
        in_stock=True,
        average_rating=4.7,
        source_run_id="source",
        event_timestamp=datetime.now(timezone.utc),
        embedding=vector,
    )


def test_safe_change_uses_native_upsert_on_active_slot():
    previous = manifest("old", {"1": "old"})
    current = manifest("new", {"1": "new", "2": "new"})
    decision = decide_publish(
        requested_mode="incremental",
        current_manifest=current,
        current_records=[record(1), record(2)],
        previous_manifest=previous,
        previous_records=[record(1)],
        active_slot="blue",
    )
    assert decision.mode == "incremental"
    assert decision.target_slot == "blue"
    assert {value.item_id for value in decision.records} == {1, 2}


def test_delete_shrink_and_model_change_force_inactive_reconciliation():
    previous = manifest("old", {"1": "old", "2": "old"})
    current = manifest("new", {"1": "new"})
    deletion = decide_publish(
        requested_mode="incremental",
        current_manifest=current,
        current_records=[record(1)],
        previous_manifest=previous,
        previous_records=[record(1), record(2)],
        active_slot="blue",
    )
    assert deletion.mode == "reconcile"
    assert deletion.target_slot == inactive_slot("blue")

    changed_contract = decide_publish(
        requested_mode="incremental",
        current_manifest=manifest("new", {"1": "old", "2": "old"}, "rev-2"),
        current_records=[record(1), record(2)],
        previous_manifest=previous,
        previous_records=[record(1), record(2)],
        active_slot="blue",
    )
    assert changed_contract.reason == "contract_change"


class FakePublisher:
    def __init__(self):
        self.rows = {"blue": {}, "green": {}}
        self.reset = []
        self.search_ok = True

    def reset_slot(self, slot):
        self.reset.append(slot)
        self.rows[slot] = {}

    def upsert(self, slot, records):
        self.rows[slot].update({value.chunk_id: value for value in records})
        return len(records)

    def collection_count(self, slot):
        return len(self.rows[slot])

    def collection_ids(self, slot, *, limit=16_384):
        return set(self.rows[slot])

    def smoke_search(self, slot, query_vector):
        return self.search_ok and bool(self.rows[slot]) and len(query_vector) == 384


class FakeStore:
    def __init__(self, artifacts, *, pointer=None):
        self.artifacts = artifacts
        self.pointer = pointer
        self.indexes = {}
        self.cas_calls = []

    def load_embeddings(self, run_id):
        return self.artifacts[run_id]

    def load_active_pointer(self):
        return self.pointer

    def write_index_manifest(self, run_id, value):
        self.indexes[run_id] = value

    def load_index_manifest(self, run_id):
        return self.indexes[run_id]

    def compare_and_swap_pointer(self, value, *, expected_etag):
        self.cas_calls.append((value, expected_etag))
        self.pointer = VersionedPointer(value, "new-etag")


def test_initial_reconcile_publish_validate_and_promote():
    artifacts = {"new": (manifest("new", {"1": "new"}), [record(1)])}
    store = FakeStore(artifacts)
    publisher = FakePublisher()

    candidate = publish_index(
        store=store, publisher=publisher, run_id="new", requested_mode="incremental"
    )
    assert candidate.status == "candidate"
    assert publisher.reset == ["blue"]

    published = validate_and_promote_index(
        store=store,
        publisher=publisher,
        run_id="new",
        promote=True,
        expected_item_count=1,
    )
    assert published.status == "published"
    assert store.pointer.pointer.pipeline_run_id == "new"
    assert store.cas_calls[0][1] is None


def test_validation_failure_records_evidence_without_pointer_change():
    artifacts = {"new": (manifest("new", {"1": "new"}), [record(1)])}
    store = FakeStore(artifacts)
    publisher = FakePublisher()
    store.indexes["new"] = IndexManifest(
        pipeline_run_id="new",
        slot="blue",
        feature_view="rag_item_chunks_blue",
        collection_name="recsys_rag_rag_item_chunks_blue",
        status="candidate",
        vector_count=0,
        unique_item_count=1,
    )
    with pytest.raises(ValueError, match="do not match"):
        validate_and_promote_index(
            store=store,
            publisher=publisher,
            run_id="new",
            promote=True,
            expected_item_count=2,
        )
    assert store.indexes["new"].status == "failed"
    assert not store.cas_calls


def test_incremental_publish_keeps_active_slot_without_false_rollback_target():
    old_pointer = ActiveIndexPointer(
        active_slot="blue",
        feature_view="rag_item_chunks_blue",
        pipeline_run_id="old",
        source_run_id="source-old",
        chunker_version="semantic_chunker_v1",
        embedding_model="intfloat/multilingual-e5-small",
        embedding_revision="rev-1",
    )
    artifacts = {
        "old": (manifest("old", {"1": "old"}), [record(1)]),
        "new": (manifest("new", {"1": "new"}), [record(1)]),
    }
    store = FakeStore(artifacts, pointer=VersionedPointer(old_pointer, "etag-old"))
    publisher = FakePublisher()
    publisher.upsert("blue", [record(1)])
    candidate = publish_index(
        store=store, publisher=publisher, run_id="new", requested_mode="incremental"
    )
    assert candidate.slot == "blue"
    assert not publisher.reset

    validate_and_promote_index(
        store=store, publisher=publisher, run_id="new", promote=True
    )
    assert store.pointer.pointer.previous_pipeline_run_id is None


def test_reconcile_promotion_supports_etag_guarded_rollback():
    old_pointer = ActiveIndexPointer(
        active_slot="blue",
        feature_view="rag_item_chunks_blue",
        pipeline_run_id="old",
        source_run_id="source-old",
        chunker_version="semantic_chunker_v1",
        embedding_model="intfloat/multilingual-e5-small",
        embedding_revision="rev-1",
    )
    artifacts = {
        "old": (manifest("old", {"1": "old"}), [record(1)]),
        "new": (manifest("new", {"1": "new"}), [record(1)]),
    }
    store = FakeStore(artifacts, pointer=VersionedPointer(old_pointer, "etag-old"))
    publisher = FakePublisher()
    publish_index(
        store=store, publisher=publisher, run_id="new", requested_mode="reconcile"
    )
    validate_and_promote_index(
        store=store, publisher=publisher, run_id="new", promote=True
    )
    assert store.pointer.pointer.previous_pipeline_run_id == "old"
    restored = rollback_active_pointer(store=store)
    assert restored.pipeline_run_id == "old"
    assert store.cas_calls[-1][1] == "new-etag"


def test_rollback_requires_previous_target():
    store = FakeStore({})
    with pytest.raises(ValueError, match="no rollback target"):
        rollback_active_pointer(store=store)
