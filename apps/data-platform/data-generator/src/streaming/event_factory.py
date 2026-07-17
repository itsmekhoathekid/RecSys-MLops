from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from streaming.types import EventBundle


class StreamEventFactory:
    """Create one clean relational event bundle without injecting problems."""

    def __init__(self, n_users: int, n_products: int):
        self.n_users = n_users
        self.n_products = n_products

    def create(
        self, counter: int, now: datetime, event_timestamp: datetime
    ) -> EventBundle:
        user_id = 900_000 + (counter % self.n_users)
        product_offset = counter % self.n_products
        product_id = 800_000 + product_offset
        category_id = 9_000 + (product_offset % 5)
        brand_id = 8_000 + (product_offset % 7)
        price_bucket = product_offset % 10
        price = Decimal(f"{20 + product_offset % 50}.99")
        event_type = ["view", "cart", "purchase"][counter % 3]
        suffix = f"{int(now.timestamp() * 1000)}-{counter}"
        session_id = f"continuous-session-{suffix}"
        request_id = f"continuous-request-{suffix}"
        impression_id = f"continuous-impression-{suffix}"
        order_id = f"continuous-order-{suffix}" if event_type == "purchase" else None

        rows: EventBundle = {
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
                "request_timestamp": event_timestamp,
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
                "impression_timestamp": event_timestamp,
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
                "event_timestamp": event_timestamp,
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
                "payload_hash": f"continuous-{suffix}",
                "event_date": event_timestamp.date(),
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
                "order_timestamp": event_timestamp,
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
