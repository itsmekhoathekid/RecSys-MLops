from __future__ import annotations

import argparse
import copy
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sinks.postgres_sink import build_upsert_sql


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


def env_float(name: str, default: str) -> float:
    return float(os.getenv(name, default))


def env_int(name: str, default: str) -> int:
    return int(os.getenv(name, default))


def build_event_rows(
    counter: int,
    now: datetime,
    n_users: int,
    n_products: int,
    *,
    rng: random.Random,
    event_timestamp: datetime | None = None,
    hot_product_ratio: float = 0.0,
    hot_product_count: int = 1,
) -> dict[str, dict[str, Any]]:
    user_id = 900_000 + (counter % n_users)
    if rng.random() < hot_product_ratio:
        product_offset = rng.randrange(max(1, min(hot_product_count, n_products)))
    else:
        product_offset = counter % n_products
    product_id = 800_000 + product_offset
    category_id = 9_000 + (product_offset % 5)
    brand_id = 8_000 + (product_offset % 7)
    price_bucket = int(product_offset % 10)
    price = Decimal(f"{20 + product_offset % 50}.99")
    event_type = ["view", "cart", "purchase"][counter % 3]
    event_ts = event_timestamp or now
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
            "request_timestamp": event_ts,
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
            "impression_timestamp": event_ts,
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
            "event_timestamp": event_ts,
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
            "event_date": event_ts.date(),
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
            "order_timestamp": event_ts,
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


def clone_event_rows(
    rows: dict[str, dict[str, Any]],
    now: datetime,
    *,
    conflicting: bool,
) -> dict[str, dict[str, Any]]:
    duplicate = copy.deepcopy(rows)
    behavior = duplicate["behavior_events"]
    behavior["created_ts"] = now
    behavior["ingestion_ts"] = now
    if conflicting:
        behavior["price"] = (Decimal(str(behavior["price"])) * Decimal("1.03")).quantize(Decimal("0.01"))
        behavior["payload_hash"] = f"{behavior['payload_hash']}-conflict-{int(now.timestamp() * 1000)}"
    for table in ("sessions", "recommendation_requests", "impressions", "orders"):
        if table in duplicate:
            row = duplicate[table]
            if "updated_ts" in row:
                row["updated_ts"] = now
            if "created_ts" in row:
                row["created_ts"] = now
    return duplicate


def main() -> int:
    import psycopg

    parser = argparse.ArgumentParser(description="Continuously insert realtime source rows into Postgres.")
    parser.add_argument("--interval-seconds", type=float, default=float(os.getenv("REALTIME_INTERVAL_SECONDS", "2")))
    parser.add_argument("--events-per-tick", type=int, default=int(os.getenv("REALTIME_EVENTS_PER_TICK", "5")))
    parser.add_argument("--max-events", type=int, default=int(os.getenv("REALTIME_MAX_EVENTS", "0")))
    parser.add_argument("--n-users", type=int, default=int(os.getenv("REALTIME_N_USERS", "20")))
    parser.add_argument("--n-products", type=int, default=int(os.getenv("REALTIME_N_PRODUCTS", "50")))
    parser.add_argument("--seed", type=int, default=env_int("REALTIME_SEED", "42"))
    parser.add_argument("--hot-product-ratio", type=float, default=env_float("REALTIME_HOT_PRODUCT_RATIO", "0.0"))
    parser.add_argument("--hot-product-count", type=int, default=env_int("REALTIME_HOT_PRODUCT_COUNT", "1"))
    parser.add_argument("--duplicate-event-rate", type=float, default=env_float("REALTIME_DUPLICATE_EVENT_RATE", "0.0"))
    parser.add_argument(
        "--conflicting-duplicate-rate",
        type=float,
        default=env_float("REALTIME_CONFLICTING_DUPLICATE_RATE", "0.0"),
    )
    parser.add_argument("--late-arrival-rate", type=float, default=env_float("REALTIME_LATE_ARRIVAL_RATE", "0.0"))
    parser.add_argument("--out-of-order-rate", type=float, default=env_float("REALTIME_OUT_OF_ORDER_RATE", "0.0"))
    parser.add_argument(
        "--late-delay-minutes-min",
        type=int,
        default=env_int("REALTIME_LATE_DELAY_MINUTES_MIN", "5"),
    )
    parser.add_argument(
        "--late-delay-minutes-max",
        type=int,
        default=env_int("REALTIME_LATE_DELAY_MINUTES_MAX", "45"),
    )
    parser.add_argument("--burst-every-n-ticks", type=int, default=env_int("REALTIME_BURST_EVERY_N_TICKS", "0"))
    parser.add_argument("--burst-multiplier", type=int, default=env_int("REALTIME_BURST_MULTIPLIER", "1"))
    args = parser.parse_args()

    rng = random.Random(args.seed)
    counter = 0
    tick = 0
    recent_rows: list[dict[str, dict[str, Any]]] = []
    with psycopg.connect(conninfo()) as connection:
        with connection.cursor() as cursor:
            bootstrap_dimensions(cursor, datetime.now(timezone.utc), args.n_users, args.n_products)
        connection.commit()

        while args.max_events <= 0 or counter < args.max_events:
            inserted = 0
            duplicated = 0
            late = 0
            out_of_order = 0
            tick += 1
            events_this_tick = args.events_per_tick
            if args.burst_every_n_ticks > 0 and tick % args.burst_every_n_ticks == 0:
                events_this_tick *= max(1, args.burst_multiplier)
            with connection.cursor() as cursor:
                for _ in range(events_this_tick):
                    if args.max_events > 0 and counter >= args.max_events:
                        break
                    now = datetime.now(timezone.utc)
                    if recent_rows and rng.random() < args.duplicate_event_rate:
                        rows = clone_event_rows(
                            rng.choice(recent_rows),
                            now,
                            conflicting=rng.random() < args.conflicting_duplicate_rate,
                        )
                        duplicated += 1
                    else:
                        event_timestamp = now
                        if rng.random() < args.late_arrival_rate:
                            delay = rng.randint(args.late_delay_minutes_min, args.late_delay_minutes_max)
                            event_timestamp = now - timedelta(minutes=delay)
                            late += 1
                        elif rng.random() < args.out_of_order_rate:
                            event_timestamp = now - timedelta(seconds=rng.randint(60, 30 * 60))
                            out_of_order += 1
                        rows = build_event_rows(
                            counter,
                            now,
                            args.n_users,
                            args.n_products,
                            rng=rng,
                            event_timestamp=event_timestamp,
                            hot_product_ratio=args.hot_product_ratio,
                            hot_product_count=args.hot_product_count,
                        )
                        recent_rows.append(copy.deepcopy(rows))
                        recent_rows = recent_rows[-1000:]
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
            print(
                json.dumps(
                    {
                        "inserted": inserted,
                        "total_events": counter,
                        "tick": tick,
                        "duplicates_emitted": duplicated,
                        "late_events_emitted": late,
                        "out_of_order_events_emitted": out_of_order,
                        "burst_tick": events_this_tick > args.events_per_tick,
                    }
                ),
                flush=True,
            )
            if args.max_events > 0 and counter >= args.max_events:
                break
            time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
