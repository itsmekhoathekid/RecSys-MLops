from __future__ import annotations

import io

import pytest
from botocore.exceptions import ClientError

from rag_data.artifact_storage import PointerConflictError, RagArtifactStore
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
