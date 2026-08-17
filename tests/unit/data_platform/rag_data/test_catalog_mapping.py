from __future__ import annotations

from decimal import Decimal

from rag_data.catalog_mapping import CatalogMapping


def mapping() -> CatalogMapping:
    return CatalogMapping.from_config(
        {
            "categories": {9000: ["Điện tử", "Tai nghe over-ear"]},
            "brands": {8000: "Sony"},
            "category_sku_slugs": {9000: "HEADPHONES"},
            "warranty_months_by_category": {9000: 24},
            "warehouses": ["SGN-01", "HAN-01", "DAD-01"],
        }
    )


def test_mapping_is_deterministic_for_same_item():
    catalog = mapping()
    first = catalog.structured_metadata(
        item_id=800000,
        brand_id=8000,
        category_id=9000,
        current_price=Decimal("20.99"),
        is_active=True,
    )
    second = catalog.structured_metadata(
        item_id=800000,
        brand_id=8000,
        category_id=9000,
        current_price=Decimal("20.99"),
        is_active=True,
    )
    assert first == second
    assert catalog.sku(800000, 8000, 9000) == "SONY-HEADPHONES-800000"
    assert first.current_price == Decimal("20.99")
    assert 10 <= first.stock_quantity <= 100
    assert first.warranty_months == 24
    assert first.warehouse_location in {"SGN-01", "HAN-01", "DAD-01"}
    assert 4.0 <= catalog.average_rating(800000) <= 4.9
    assert 50 <= catalog.total_reviews(800000) <= 500


def test_mapping_rejects_unknown_taxonomy():
    catalog = mapping()
    try:
        catalog.category_path(9999)
    except ValueError as exc:
        assert "category_id=9999" in str(exc)
    else:
        raise AssertionError("unknown category should fail")
