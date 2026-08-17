from __future__ import annotations

import os
import uuid

import boto3
import pytest

from rag_data.artifact_storage import PointerConflictError, RagArtifactStore
from rag_data.pipeline_contracts import ActiveIndexPointer


pytestmark = pytest.mark.skipif(
    not os.getenv("RAG_TEST_MINIO_ENDPOINT"),
    reason="real MinIO integration endpoint is not configured",
)


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


def test_minio_active_pointer_has_real_conditional_write_semantics():
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["RAG_TEST_MINIO_ENDPOINT"],
        aws_access_key_id="minio",
        aws_secret_access_key="minio123",
        region_name="us-east-1",
    )
    bucket = f"rag-ci-{uuid.uuid4().hex[:16]}"
    client.create_bucket(Bucket=bucket)
    store = RagArtifactStore(client=client, bucket=bucket)
    first_etag = store.compare_and_swap_pointer(pointer("run-1"), expected_etag=None)
    assert store.load_active_pointer().etag == first_etag
    store.compare_and_swap_pointer(pointer("run-2"), expected_etag=first_etag)
    with pytest.raises(PointerConflictError):
        store.compare_and_swap_pointer(pointer("stale"), expected_etag=first_etag)
