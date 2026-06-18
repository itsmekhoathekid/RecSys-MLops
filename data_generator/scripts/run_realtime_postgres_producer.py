from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from data_generator.sinks.postgres_sink import build_upsert_sql


def conninfo() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'recsys')} "
        f"user={os.getenv('POSTGRES_USER', 'recsys')} "
        f"password={os.getenv('POSTGRES_PASSWORD', 'recsys')}"
    )


def upsert(cursor: Any, table_name: str, row: dict[str, Any]) -> None:
    columns = list(row.keys())
    cursor.execute(build_upsert_sql(table_name, columns), [row[column] for column in columns])


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
                "segment": "local_poc",
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
                "price_bucket": int(offset % 10),
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
                "price_bucket": int(offset % 10),
                "is_active": True,
                "created_ts": now,
            },
        )


def build_event_rows(counter: int, now: datetime, n_users: int, n_products: int) -> dict[str, dict[str, Any]]:
    user_id = 900_000 + (counter % n_users)
    product_offset = counter % n_products
    product_id = 800_000 + product_offset
    category_id = 9_000 + (product_offset % 5)
    brand_id = 8_000 + (product_offset % 7)
    price_bucket = int(product_offset % 10)
    price = Decimal(f"{20 + product_offset % 50}.99")
    event_type = ["view", "cart", "purchase"][counter % 3]
    suffix = f"{int(now.timestamp() * 1000)}-{counter}"
    session_id = f"continuous-session-{suffix}"
    request_id = f"continuous-request-{suffix}"
    impression_id = f"continuous-impression-{suffix}"
    order_id = f"continuous-order-{suffix}" if event_type == "purchase" else None
    payload_hash = f"continuous-{suffix}"

    rows = {
        "sessions": {
            "session_id": session_id,
            "user_id": user_id,
            "session_start_ts": now,
            "session_end_ts": now + timedelta(minutes=5),
            "entry_source": "continuous_local",
            "device_type": "web",
            "campaign_id": "continuous",
            "session_end_reason": "active",
            "created_ts": now,
            "updated_ts": now,
        },
        "recommendation_requests": {
            "request_id": request_id,
            "user_id": user_id,
            "session_id": session_id,
            "request_timestamp": now,
            "surface": "home",
            "context_product_id": product_id,
            "context_category_id": category_id,
            "device_type": "web",
            "source": "continuous_local",
            "campaign_id": "continuous",
            "created_ts": now,
            "schema_version": 2,
        },
        "impressions": {
            "impression_id": impression_id,
            "request_id": request_id,
            "user_id": user_id,
            "session_id": session_id,
            "impression_timestamp": now,
            "candidate_product_id": product_id,
            "rank_position": 1,
            "candidate_source": "continuous_local",
            "retrieval_score": 1.0,
            "ranking_score": 1.0,
            "surface": "home",
            "is_clicked": event_type in {"cart", "purchase"},
            "created_ts": now,
            "schema_version": 2,
        },
        "behavior_events": {
            "event_id": f"continuous-event-{suffix}",
            "event_timestamp": now,
            "created_ts": now,
            "ingestion_ts": now,
            "user_id": user_id,
            "session_id": session_id,
            "request_id": request_id,
            "impression_id": impression_id,
            "event_type": event_type,
            "product_id": product_id,
            "category_id": category_id,
            "brand_id": brand_id,
            "price": price,
            "price_bucket": price_bucket,
            "quantity": 1,
            "device_type": "web",
            "source": "continuous_local",
            "campaign_id": "continuous",
            "page_context": "home",
            "rank_position": 1,
            "order_id": order_id,
            "payload_hash": payload_hash,
            "event_date": now.date(),
            "schema_version": 2,
            "drift_enabled": False,
            "drift_scenario": "none",
            "drift_phase": "none",
            "drift_factor": 1.0,
        },
    }
    if event_type == "purchase":
        rows["orders"] = {
            "order_id": order_id,
            "user_id": user_id,
            "session_id": session_id,
            "order_timestamp": now,
            "status": "paid",
            "gross_amount": price,
            "discount_amount": Decimal("0.00"),
            "net_amount": price,
            "coupon_code": "",
            "payment_method": "card",
            "shipping_city": "HCMC",
            "paid_ts": now,
            "cancelled_ts": None,
            "refunded_ts": None,
            "created_ts": now,
            "updated_ts": now,
            "drift_enabled": False,
            "drift_scenario": "none",
            "drift_phase": "none",
            "drift_factor": 1.0,
        }
        rows["order_items"] = {
            "order_item_id": f"continuous-order-item-{suffix}",
            "order_id": order_id,
            "product_id": product_id,
            "quantity": 1,
            "unit_price": price,
            "discount_amount": Decimal("0.00"),
            "line_amount": price,
            "created_ts": now,
        }
    return rows


def main() -> int:
    import psycopg

    parser = argparse.ArgumentParser(description="Continuously insert realtime source rows into Postgres.")
    parser.add_argument("--interval-seconds", type=float, default=float(os.getenv("REALTIME_INTERVAL_SECONDS", "2")))
    parser.add_argument("--events-per-tick", type=int, default=int(os.getenv("REALTIME_EVENTS_PER_TICK", "5")))
    parser.add_argument("--max-events", type=int, default=int(os.getenv("REALTIME_MAX_EVENTS", "0")))
    parser.add_argument("--n-users", type=int, default=20)
    parser.add_argument("--n-products", type=int, default=50)
    args = parser.parse_args()

    counter = 0
    with psycopg.connect(conninfo()) as connection:
        with connection.cursor() as cursor:
            bootstrap_dimensions(cursor, datetime.now(timezone.utc), args.n_users, args.n_products)
        connection.commit()

        while args.max_events <= 0 or counter < args.max_events:
            inserted = 0
            with connection.cursor() as cursor:
                for _ in range(args.events_per_tick):
                    if args.max_events > 0 and counter >= args.max_events:
                        break
                    rows = build_event_rows(
                        counter,
                        datetime.now(timezone.utc),
                        args.n_users,
                        args.n_products,
                    )
                    for table in [
                        "sessions",
                        "recommendation_requests",
                        "impressions",
                        "behavior_events",
                        "orders",
                        "order_items",
                    ]:
                        if table in rows:
                            upsert(cursor, table, rows[table])
                    counter += 1
                    inserted += 1
            connection.commit()
            print(json.dumps({"inserted": inserted, "total_events": counter}), flush=True)
            if args.max_events > 0 and counter >= args.max_events:
                break
            time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
