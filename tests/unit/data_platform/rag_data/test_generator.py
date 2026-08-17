from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from threading import Barrier

from rag_data.catalog_mapping import CatalogMapping
from rag_data.contracts import GeneratedItemContent
from rag_data.generator import ItemMetadataGenerator, ProductRow, compose_document
from rag_data.storage import RunState


def mapping() -> CatalogMapping:
    return CatalogMapping.from_config(
        {
            "categories": {9000: ["Điện tử", "Thiết bị âm thanh", "Tai nghe"]},
            "brands": {8000: "Sony"},
            "category_sku_slugs": {9000: "HEADPHONES"},
            "warranty_months_by_category": {9000: 24},
            "warehouses": ["SGN-01", "HAN-01", "DAD-01"],
        }
    )


def product(item_id=800000) -> ProductRow:
    return ProductRow(
        product_id=item_id,
        product_name=f"Continuous Product {item_id}",
        category_id=9000,
        category_code="cat-9000",
        brand_id=8000,
        brand_name="Brand 8000",
        current_price=Decimal("20.99"),
        is_active=True,
        updated_ts=datetime.now(timezone.utc),
    )


def generated() -> GeneratedItemContent:
    return GeneratedItemContent.model_validate(
        {
            "title": "Tai nghe synthetic",
            "description": "Mô tả demo.",
            "specifications": {"battery": "30 giờ"},
            "usage_instructions": "Bật nguồn.",
            "reviews": [
                {"content": "Tốt.", "sentiment_aspects": {"sound": "positive"}},
                {"content": "Êm.", "sentiment_aspects": {"comfort": "positive"}},
            ],
            "qna_pairs": [{"question": "Có Bluetooth?", "answer": "Có."}],
        }
    )


def test_compose_document_keeps_exact_source_price_and_protects_metadata():
    document = compose_document(product(), generated(), mapping())
    assert document.item_id == 800000
    assert document.structured_metadata.current_price == Decimal("20.99")
    assert "currency" not in type(document.structured_metadata).model_fields
    assert document.sku == "SONY-HEADPHONES-800000"
    assert [review.review_id for review in document.reviews_and_qna.sample_reviews] == [
        "rev_800000_01",
        "rev_800000_02",
    ]


class Source:
    def __init__(self, products):
        self.products = products

    def fetch_products(self, *, item_ids=None, limit=0):
        rows = [
            row for row in self.products if not item_ids or row.product_id in item_ids
        ]
        return rows[:limit] if limit else rows


class Llm:
    model = "openai/gpt-oss-120b"

    def __init__(self, fail_item=None):
        self.fail_item = fail_item

    def generate(self, prompt):
        if self.fail_item and str(self.fail_item) in prompt:
            raise RuntimeError("synthetic failure")
        return generated()


class Storage:
    run_id = "test-run"

    def __init__(self):
        self.checkpoints = 0

    def start(self, *, manifest, force=False):
        return RunState(items={}, failures={}, manifest=manifest)

    def add_items(self, state, items):
        for item in items:
            state.items[item.item_id] = item
            state.failures.pop(item.item_id, None)

    def add_failures(self, state, failures):
        for failure in failures:
            state.failures[failure.item_id] = failure

    def checkpoint(self, state):
        self.checkpoints += 1


def test_generator_isolates_item_failure_and_returns_partial_manifest():
    storage = Storage()
    runner = ItemMetadataGenerator(
        source=Source([product(800000), product(800001)]),
        content_generator=Llm(fail_item=800001),
        mapping=mapping(),
        storage=storage,
        checkpoint_every=1,
    )
    state = runner.run()
    assert set(state.items) == {800000}
    assert set(state.failures) == {800001}
    assert state.manifest.status == "partial"
    assert state.manifest.generated_count == 1
    assert state.manifest.failed_count == 1
    assert storage.checkpoints == 3


def test_generator_can_generate_products_concurrently():
    barrier = Barrier(2)

    class ConcurrentLlm:
        model = "openai/gpt-oss-120b"

        def generate(self, prompt):
            barrier.wait(timeout=2)
            return generated()

    runner = ItemMetadataGenerator(
        source=Source([product(800000), product(800001)]),
        content_generator=ConcurrentLlm(),
        mapping=mapping(),
        storage=Storage(),
        checkpoint_every=2,
        workers=2,
    )

    state = runner.run()

    assert state.manifest.status == "complete"
    assert set(state.items) == {800000, 800001}
    assert state.manifest.finish_reason_counts == {"unknown": 2}
