from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.models import EventRequest, Product, RecommendationItem, User


class RecordNotFoundError(Exception):
    pass


class IdempotencyConflictError(Exception):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def event_type_id(action: str) -> int:
    return {"view": 1, "cart": 2, "purchase": 3}[action]


def canonical_payload_hash(request: EventRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def event_id_for(request: EventRequest, header_key: str | None = None) -> str:
    key = header_key or request.idempotency_key
    if not key:
        return str(uuid.uuid4())
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://recsys-mlops.site/events/{key}"))


class DemoRepository:
    def __init__(self, pool: ConnectionPool | None = None) -> None:
        self.pool = pool or ConnectionPool(
            conninfo=(
                f"host={os.getenv('POSTGRES_HOST', 'source-postgres.recsys-dataflow.svc.cluster.local')} "
                f"port={os.getenv('POSTGRES_PORT', '5432')} "
                f"dbname={os.getenv('POSTGRES_DB', 'recsys')} "
                f"user={os.getenv('POSTGRES_USER', 'recsys')} "
                f"password={os.getenv('POSTGRES_PASSWORD', '')} "
                f"connect_timeout={os.getenv('POSTGRES_CONNECT_TIMEOUT_SECONDS', '5')}"
            ),
            min_size=max(1, int(os.getenv("POSTGRES_POOL_MIN_SIZE", "1"))),
            max_size=max(1, int(os.getenv("POSTGRES_POOL_MAX_SIZE", "8"))),
            timeout=float(os.getenv("POSTGRES_POOL_TIMEOUT_SECONDS", "5")),
            open=False,
            kwargs={"row_factory": dict_row},
        )

    def open(self) -> None:
        self.pool.open(wait=True, timeout=float(os.getenv("POSTGRES_STARTUP_TIMEOUT_SECONDS", "15")))

    def close(self) -> None:
        self.pool.close()

    def ping(self) -> None:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

    def users(self, limit: int, offset: int) -> tuple[list[User], int]:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) AS total FROM users WHERE is_active IS TRUE")
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                """
                SELECT user_id, segment, city
                FROM users
                WHERE is_active IS TRUE
                ORDER BY user_id
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            return [User.model_validate(row) for row in cursor.fetchall()], total

    def products(self, limit: int, offset: int) -> tuple[list[Product], int]:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) AS total FROM products WHERE is_active IS TRUE")
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                """
                SELECT product_id, product_name, category_id, category_code,
                       brand_id, brand_name, current_price, price_bucket
                FROM products
                WHERE is_active IS TRUE
                ORDER BY popularity_weight DESC NULLS LAST, product_id
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            return [self._product(row) for row in cursor.fetchall()], total

    def user_exists(self, user_id: int) -> bool:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM users WHERE user_id=%s AND is_active IS TRUE", (user_id,))
            return cursor.fetchone() is not None

    def product(self, product_id: int) -> Product | None:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT product_id, product_name, category_id, category_code,
                       brand_id, brand_name, current_price, price_bucket
                FROM products WHERE product_id=%s AND is_active IS TRUE
                """,
                (product_id,),
            )
            row = cursor.fetchone()
            return self._product(row) if row else None

    def products_by_id(self, product_ids: list[int]) -> dict[int, Product]:
        if not product_ids:
            return {}
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT product_id, product_name, category_id, category_code,
                       brand_id, brand_name, current_price, price_bucket
                FROM products WHERE product_id = ANY(%s)
                """,
                (product_ids,),
            )
            products = [self._product(row) for row in cursor.fetchall()]
            return {product.product_id: product for product in products}

    def record_event(
        self,
        request: EventRequest,
        event_id: str,
        payload_hash: str,
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        correlation_id = request.request_id or f"web-event-{event_id}"
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT event_id, event_timestamp, request_id, payload_hash
                    FROM behavior_events WHERE event_id=%s
                    ORDER BY created_ts DESC LIMIT 1
                    """,
                    (event_id,),
                )
                existing = cursor.fetchone()
                if existing:
                    if existing["payload_hash"] != payload_hash:
                        raise IdempotencyConflictError(event_id)
                    return existing, True

                cursor.execute(
                    "SELECT user_id FROM users WHERE user_id=%s AND is_active IS TRUE",
                    (request.user_id,),
                )
                if cursor.fetchone() is None:
                    raise RecordNotFoundError(f"user {request.user_id}")
                cursor.execute(
                    """
                    SELECT product_id, product_name, category_id, category_code,
                           brand_id, brand_name, current_price, price_bucket
                    FROM products WHERE product_id=%s AND is_active IS TRUE
                    """,
                    (request.product_id,),
                )
                product_row = cursor.fetchone()
                if product_row is None:
                    raise RecordNotFoundError(f"product {request.product_id}")

                self._upsert_session(cursor, request.session_id, request.user_id, now)
                order_id: str | None = None
                if request.action == "purchase":
                    order_id = f"web-order-{event_id}"
                    self._insert_purchase(cursor, order_id, request, product_row, now)

                if request.impression_id:
                    cursor.execute(
                        """
                        UPDATE impressions SET is_clicked=TRUE
                        WHERE impression_id=%s AND user_id=%s AND candidate_product_id=%s
                        """,
                        (request.impression_id, request.user_id, request.product_id),
                    )

                cursor.execute(
                    """
                    INSERT INTO behavior_events (
                      event_id, event_timestamp, created_ts, ingestion_ts, user_id,
                      session_id, request_id, impression_id, event_type, product_id,
                      category_id, brand_id, price, price_bucket, quantity, device_type,
                      source, campaign_id, page_context, rank_position, order_id,
                      payload_hash, event_date, schema_version, drift_enabled,
                      drift_scenario, drift_phase, drift_factor
                    ) VALUES (
                      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                    )
                    """,
                    (
                        event_id,
                        now,
                        now,
                        now,
                        request.user_id,
                        request.session_id,
                        correlation_id,
                        request.impression_id,
                        request.action,
                        request.product_id,
                        product_row["category_id"],
                        product_row["brand_id"],
                        product_row["current_price"],
                        product_row["price_bucket"],
                        request.quantity,
                        "web",
                        "recsys_demo_web",
                        "production_demo",
                        "catalog" if not request.impression_id else "recommendations",
                        None,
                        order_id,
                        payload_hash,
                        now.date(),
                        2,
                        False,
                        "none",
                        "none",
                        1.0,
                    ),
                )
                cursor.execute(
                    "UPDATE users SET last_active_ts=%s, updated_ts=%s WHERE user_id=%s",
                    (now, now, request.user_id),
                )
            connection.commit()
        return {
            "event_id": event_id,
            "event_timestamp": now,
            "request_id": correlation_id,
            "payload_hash": payload_hash,
        }, False

    def event(self, event_id: str) -> dict[str, Any] | None:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_id, event_timestamp, user_id, product_id, event_type,
                       request_id, impression_id
                FROM behavior_events WHERE event_id=%s
                ORDER BY created_ts DESC LIMIT 1
                """,
                (event_id,),
            )
            return cursor.fetchone()

    def record_recommendation_request(self, user_id: int, session_id: str, request_id: str) -> None:
        now = utc_now()
        with self.pool.connection() as connection, connection.cursor() as cursor:
            self._upsert_session(cursor, session_id, user_id, now)
            cursor.execute(
                """
                INSERT INTO recommendation_requests (
                  request_id, user_id, session_id, request_timestamp, surface,
                  context_product_id, context_category_id, device_type, source,
                  campaign_id, created_ts, schema_version
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (request_id) DO NOTHING
                """,
                (
                    request_id,
                    user_id,
                    session_id,
                    now,
                    "home",
                    None,
                    None,
                    "web",
                    "recsys_demo_web",
                    "production_demo",
                    now,
                    2,
                ),
            )
            connection.commit()

    def record_impressions(
        self,
        request_id: str,
        user_id: int,
        session_id: str,
        items: list[RecommendationItem],
    ) -> None:
        now = utc_now()
        with self.pool.connection() as connection, connection.cursor() as cursor:
            for rank, item in enumerate(items, start=1):
                cursor.execute(
                    """
                    INSERT INTO impressions (
                      impression_id, request_id, user_id, session_id,
                      impression_timestamp, candidate_product_id, rank_position,
                      candidate_source, retrieval_score, ranking_score, surface,
                      is_clicked, created_ts, schema_version
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (impression_id) DO NOTHING
                    """,
                    (
                        item.impression_id,
                        request_id,
                        user_id,
                        session_id,
                        now,
                        item.item_id,
                        rank,
                        "bst_online_ranking",
                        item.score,
                        item.score,
                        "home",
                        False,
                        now,
                        2,
                    ),
                )
            connection.commit()

    @staticmethod
    def _product(row: dict[str, Any]) -> Product:
        payload = dict(row)
        payload["current_price"] = float(payload.get("current_price") or 0)
        return Product.model_validate(payload)

    @staticmethod
    def _upsert_session(cursor: Any, session_id: str, user_id: int, now: datetime) -> None:
        cursor.execute(
            """
            INSERT INTO sessions (
              session_id, user_id, session_start_ts, session_end_ts, entry_source,
              device_type, campaign_id, session_end_reason, created_ts, updated_ts
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (session_id) DO UPDATE SET
              session_end_ts=EXCLUDED.session_end_ts,
              updated_ts=EXCLUDED.updated_ts
            """,
            (
                session_id,
                user_id,
                now,
                now + timedelta(minutes=30),
                "recsys_demo_web",
                "web",
                "production_demo",
                "active",
                now,
                now,
            ),
        )

    @staticmethod
    def _insert_purchase(
        cursor: Any,
        order_id: str,
        request: EventRequest,
        product: dict[str, Any],
        now: datetime,
    ) -> None:
        unit_price = Decimal(product["current_price"])
        total = unit_price * request.quantity
        cursor.execute(
            """
            INSERT INTO orders (
              order_id, user_id, session_id, order_timestamp, status, gross_amount,
              discount_amount, net_amount, coupon_code, payment_method, shipping_city,
              paid_ts, cancelled_ts, refunded_ts, created_ts, updated_ts,
              drift_enabled, drift_scenario, drift_phase, drift_factor
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                order_id,
                request.user_id,
                request.session_id,
                now,
                "paid",
                total,
                Decimal("0.00"),
                total,
                "",
                "demo",
                "HCMC",
                now,
                None,
                None,
                now,
                now,
                False,
                "none",
                "none",
                1.0,
            ),
        )
        cursor.execute(
            """
            INSERT INTO order_items (
              order_item_id, order_id, product_id, quantity, unit_price,
              discount_amount, line_amount, created_ts
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                f"web-order-item-{uuid.uuid4()}",
                order_id,
                request.product_id,
                request.quantity,
                unit_price,
                Decimal("0.00"),
                total,
                now,
            ),
        )
