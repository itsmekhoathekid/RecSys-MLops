from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

from rag_data.catalog_mapping import CatalogMapping
from rag_data.contracts import RunManifest
from rag_data.generator import compose_document
from rag_data.storage import CompletedRunError, IncompatibleRunError, MinioRunStorage
from test_generator import generated, product


class FakeS3:
    def __init__(self):
        self.objects = {}

    def get_object(self, *, Bucket, Key):
        try:
            body = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject"
            ) from exc
        return {"Body": io.BytesIO(body)}

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = Body


def mapping() -> CatalogMapping:
    return CatalogMapping.from_config(
        {
            "categories": {9000: ["Điện tử", "Tai nghe"]},
            "brands": {8000: "Sony"},
            "category_sku_slugs": {9000: "HEADPHONES"},
            "warranty_months_by_category": {9000: 24},
            "warehouses": ["SGN-01"],
        }
    )


def manifest(status="running") -> RunManifest:
    return RunManifest(
        run_id="run-1",
        status=status,
        model="openai/gpt-oss-120b",
        prompt_version="rag_item_content_v1",
        started_at=datetime.now(timezone.utc),
    )


def test_checkpoint_resume_and_duplicate_id_protection():
    s3 = FakeS3()
    storage = MinioRunStorage(client=s3, bucket="recsys-lakehouse", run_id="run-1")
    state = storage.start(manifest=manifest())
    item = compose_document(product(), generated(), mapping())
    storage.add_items(state, [item, item])
    state.manifest = state.manifest.refreshed(generated_count=1, status="partial")
    storage.checkpoint(state)

    resumed = storage.load()
    assert resumed.completed_item_ids == {800000}
    assert resumed.items[800000].structured_metadata.current_price == Decimal("20.99")
    raw = s3.objects[("recsys-lakehouse", storage.key("items.jsonl"))].decode()
    assert len(raw.splitlines()) == 1
    assert isinstance(json.loads(raw)["structured_metadata"]["current_price"], float)


def test_completed_run_requires_force_to_overwrite():
    s3 = FakeS3()
    storage = MinioRunStorage(client=s3, bucket="recsys-lakehouse", run_id="run-1")
    state = storage.start(manifest=manifest())
    state.manifest = state.manifest.refreshed(status="complete")
    storage.checkpoint(state)

    with pytest.raises(CompletedRunError):
        storage.start(manifest=manifest())

    reset = storage.start(manifest=manifest(), force=True)
    assert reset.items == {}
    assert reset.manifest.status == "running"


def test_partial_run_cannot_mix_prompt_or_model_versions():
    s3 = FakeS3()
    storage = MinioRunStorage(client=s3, bucket="recsys-lakehouse", run_id="run-1")
    state = storage.start(manifest=manifest())
    state.manifest = state.manifest.refreshed(status="partial")
    storage.checkpoint(state)

    changed = manifest().model_copy(update={"prompt_version": "rag_item_content_v2"})
    with pytest.raises(IncompatibleRunError, match="--force"):
        storage.start(manifest=changed)


def test_run_id_cannot_escape_raw_prefix():
    with pytest.raises(ValueError, match="run_id"):
        MinioRunStorage(client=FakeS3(), bucket="bucket", run_id="../escape")
