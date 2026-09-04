from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from rag_data.artifact_storage import PointerConflictError, RagArtifactStore
from rag_data.contracts import RunManifest
from rag_data.pipeline_contracts import ActiveIndexPointer


class ConditionalS3:
    def __init__(self):
        self.objects = {}
        self.version = 0

    def get_object(self, *, Bucket, Key):
        try:
            body, etag = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject") from exc
        return {"Body": io.BytesIO(body), "ETag": f'"{etag}"'}

    def put_object(self, *, Bucket, Key, Body, ContentType, **conditions):
        current = self.objects.get((Bucket, Key))
        if conditions.get("IfNoneMatch") == "*" and current is not None:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        if "IfMatch" in conditions and (
            current is None or current[1] != conditions["IfMatch"]
        ):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.version += 1
        etag = f"etag-{self.version}"
        self.objects[(Bucket, Key)] = (Body, etag)
        return {"ETag": f'"{etag}"'}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(
            key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix)
        )
        start = int(ContinuationToken or 0)
        page = keys[start : start + 2]
        next_offset = start + len(page)
        return {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": next_offset < len(keys),
            "NextContinuationToken": str(next_offset),
        }


def pointer(run_id: str) -> ActiveIndexPointer:
    return ActiveIndexPointer(
        active_slot="blue",
        feature_view="rag_item_chunks_blue",
        pipeline_run_id=run_id,
        source_run_id="source",
        chunker_version="semantic_chunker_v1",
        embedding_model="intfloat/multilingual-e5-small",
        embedding_revision="revision",
    )


def test_active_pointer_uses_etag_compare_and_swap():
    store = RagArtifactStore(client=ConditionalS3(), bucket="lake")
    assert store.compare_and_swap_pointer(pointer("run-1"), expected_etag=None) == "etag-1"
    active = store.load_active_pointer()
    assert active.pointer.pipeline_run_id == "run-1"
    assert active.etag == "etag-1"

    assert store.compare_and_swap_pointer(pointer("run-2"), expected_etag="etag-1") == "etag-2"
    with pytest.raises(PointerConflictError):
        store.compare_and_swap_pointer(pointer("stale"), expected_etag="etag-1")


def test_latest_complete_source_run_uses_manifest_timestamp_and_pagination():
    client = ConditionalS3()
    now = datetime.now(timezone.utc)
    for run_id, status, updated_at in (
        ("older", "complete", now - timedelta(days=1)),
        ("newest-running", "running", now + timedelta(days=1)),
        ("newest-complete", "complete", now),
    ):
        manifest = RunManifest(
            run_id=run_id,
            status=status,
            model="model",
            prompt_version="v1",
            updated_at=updated_at,
        )
        client.put_object(
            Bucket="lake",
            Key=f"raw/{run_id}/rag_item_documents/manifest.json",
            Body=json.dumps(manifest.model_dump(mode="json")).encode(),
            ContentType="application/json",
        )
    store = RagArtifactStore(client=client, bucket="lake")
    assert store.latest_complete_source_run() == "newest-complete"
