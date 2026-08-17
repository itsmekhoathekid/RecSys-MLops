"""Deterministically map source product IDs into structured catalog metadata.

The mapping is repeatable for a configuration version and never invokes an
LLM. Unknown category or brand IDs fail the item instead of inventing filters.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from rag_data.contracts import StructuredMetadata


CATALOG_MAPPING_VERSION = "catalog_mapping_v1"


def _slug(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    )
    return re.sub(r"[^A-Za-z0-9]+", "-", ascii_value).strip("-").upper()


@dataclass(frozen=True)
class CatalogMapping:
    """Versioned taxonomy and deterministic synthetic catalog rules."""

    categories: dict[int, list[str]]
    brands: dict[int, str]
    warranty_months_by_category: dict[int, int]
    warehouses: tuple[str, ...]
    category_sku_slugs: dict[int, str]

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "CatalogMapping":
        """Build a mapping from the validated YAML catalog section."""

        return cls(
            categories={
                int(key): list(value) for key, value in config["categories"].items()
            },
            brands={int(key): str(value) for key, value in config["brands"].items()},
            warranty_months_by_category={
                int(key): int(value)
                for key, value in config["warranty_months_by_category"].items()
            },
            warehouses=tuple(config["warehouses"]),
            category_sku_slugs={
                int(key): str(value)
                for key, value in config.get("category_sku_slugs", {}).items()
            },
        )

    def brand(self, brand_id: int) -> str:
        """Return a mapped brand or raise when the source ID is unknown."""

        try:
            return self.brands[brand_id]
        except KeyError as exc:
            raise ValueError(f"No catalog mapping for brand_id={brand_id}") from exc

    def category_path(self, category_id: int) -> list[str]:
        """Return a defensive copy of a mapped category hierarchy."""

        try:
            return list(self.categories[category_id])
        except KeyError as exc:
            raise ValueError(
                f"No catalog mapping for category_id={category_id}"
            ) from exc

    def sku(self, item_id: int, brand_id: int, category_id: int) -> str:
        """Create a stable SKU from mapped brand/category and item ID."""

        category_slug = self.category_sku_slugs.get(
            category_id, _slug(self.category_path(category_id)[-1])
        )
        return f"{_slug(self.brand(brand_id))}-{_slug(category_slug)}-{item_id}"

    @staticmethod
    def stock_quantity(item_id: int) -> int:
        """Derive a stable demo stock quantity in the inclusive 10-100 range."""

        return 10 + item_id % 91

    def warehouse_location(self, item_id: int) -> str:
        """Cycle item IDs through configured warehouses deterministically."""

        if not self.warehouses:
            raise ValueError("warehouses must not be empty")
        return self.warehouses[item_id % len(self.warehouses)]

    def warranty_months(self, category_id: int) -> int:
        """Return the configured category warranty or fail closed."""

        try:
            return self.warranty_months_by_category[category_id]
        except KeyError as exc:
            raise ValueError(
                f"No warranty mapping for category_id={category_id}"
            ) from exc

    @staticmethod
    def average_rating(item_id: int) -> float:
        """Derive a stable rating in the inclusive 4.0-4.9 range."""

        return float(Decimal("4.0") + Decimal(item_id % 10) / Decimal(10))

    @staticmethod
    def total_reviews(item_id: int) -> int:
        """Derive a stable review count in the inclusive 50-500 range."""

        return 50 + item_id % 451

    @staticmethod
    def review_ratings(item_id: int) -> tuple[int, int]:
        """Return deterministic ratings for the two generated review records."""

        return (5 if item_id % 2 == 0 else 4, 4 if item_id % 3 else 5)

    def structured_metadata(
        self,
        *,
        item_id: int,
        brand_id: int,
        category_id: int,
        current_price: Decimal,
        is_active: bool,
    ) -> StructuredMetadata:
        """Compose all non-LLM metadata while preserving source price exactly."""

        stock = self.stock_quantity(item_id)
        return StructuredMetadata(
            brand=self.brand(brand_id),
            category_path=self.category_path(category_id),
            current_price=current_price,
            in_stock=is_active and stock > 0,
            stock_quantity=stock,
            warranty_months=self.warranty_months(category_id),
            warehouse_location=self.warehouse_location(item_id),
        )
