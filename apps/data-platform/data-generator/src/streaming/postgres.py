from __future__ import annotations

import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sinks.postgres_sink import build_upsert_sql
from streaming.types import EventBundle


def conninfo() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'recsys')} "
        f"user={os.getenv('POSTGRES_USER', 'recsys')} "
        f"password={os.getenv('POSTGRES_PASSWORD', 'recsys')}"
    )


def upsert(cursor: Any, table_name: str, row: dict[str, Any]) -> None:
    columns = list(row)
    cursor.execute(
        build_upsert_sql(table_name, columns),
        [row[column] for column in columns],
    )


def write_bundle(cursor: Any, rows: EventBundle) -> None:
    for table in (
        "sessions",
        "recommendation_requests",
        "impressions",
        "behavior_events",
        "orders",
        "order_items",
    ):
        if table in rows:
            upsert(cursor, table, rows[table])


def bootstrap_dimensions(cursor: Any, now: datetime, n_users: int, n_products: int) -> None:
    for offset in range(n_users):
        user_id = 900_000 + offset
        category_id = 9_000 + (offset % 5)
        brand_id = 8_000 + (offset % 7)
        upsert(
            cursor,
            "users",
            {
                "user_id": user_id,
                "signup_ts": now - timedelta(days=30),
                "signup_channel": "continuous_local",
                "city": "HCMC",
                "country": "VN",
                "segment": "native_lakehouse",
                "age_bucket": 3,
                "preferred_category_id": category_id,
                "preferred_brand_id": brand_id,
                "price_sensitivity": 0.5,
                "user_lifecycle_state": "active",
                "last_active_ts": now,
                "is_active": True,
                "created_ts": now,
                "updated_ts": now,
            },
        )
        upsert(
            cursor,
            "user_preferences",
            {
                "user_id": user_id,
                "category_id": category_id,
                "brand_id": brand_id,
                "preference_weight": 1.0,
                "source": "continuous_local",
                "created_ts": now,
                "updated_ts": now,
            },
        )

    for offset in range(n_products):
        product_id = 800_000 + offset
        category_id = 9_000 + (offset % 5)
        brand_id = 8_000 + (offset % 7)
        price = Decimal(f"{20 + offset % 50}.99")
        upsert(
            cursor,
            "products",
            {
                "product_id": product_id,
                "product_name": f"Continuous Product {product_id}",
                "category_id": category_id,
                "category_code": f"cat-{category_id}",
                "brand_id": brand_id,
                "brand_name": f"Brand {brand_id}",
                "base_price": price,
                "current_price": price,
                "price_bucket": offset % 10,
                "popularity_weight": 1.0 + (offset % 5) / 10.0,
                "is_active": True,
                "created_ts": now,
                "updated_ts": now,
            },
        )
        upsert(
            cursor,
            "product_snapshots",
            {
                "product_id": product_id,
                "valid_from": now,
                "valid_to": now + timedelta(days=365),
                "category_id": category_id,
                "category_code": f"cat-{category_id}",
                "brand_id": brand_id,
                "brand_name": f"Brand {brand_id}",
                "current_price": price,
                "price_bucket": offset % 10,
                "is_active": True,
                "created_ts": now,
            },
        )
