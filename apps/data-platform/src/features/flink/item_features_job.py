from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from features.flink.time_utils import isoformat_utc, parse_event_time


@dataclass
class ItemFeatureState:
    events_by_product: dict[int, deque[dict[str, Any]]] = field(default_factory=lambda: defaultdict(deque))

    def update(self, event: dict[str, Any]) -> dict[str, Any]:
        product_id = int(event["product_id"])
        ts = parse_event_time(event["event_timestamp"])
        history = self.events_by_product[product_id]
        history.append(event)
        while history and parse_event_time(history[0]["event_timestamp"]) < ts - timedelta(days=7):
            history.popleft()
        rows = list(history)
        h1_start = ts - timedelta(hours=1)
        h24_start = ts - timedelta(hours=24)
        h1 = [row for row in rows if parse_event_time(row["event_timestamp"]) > h1_start]
        h24 = [row for row in rows if parse_event_time(row["event_timestamp"]) > h24_start]
        views_7d = sum(1 for row in rows if row["event_type"] == "view")
        purchases_7d = sum(1 for row in rows if row["event_type"] == "purchase")
        popularity = (
            sum(1 for row in h24 if row["event_type"] == "view")
            + 3 * sum(1 for row in h24 if row["event_type"] == "cart")
            + 10 * sum(1 for row in h24 if row["event_type"] == "purchase")
        )
        return {
            "product_id": product_id,
            "category_id": int(event["category_id"]),
            "brand_id": int(event["brand_id"]),
            "price_bucket": int(event["price_bucket"]),
            "is_active": True,
            "views_1h": sum(1 for row in h1 if row["event_type"] == "view"),
            "views_24h": sum(1 for row in h24 if row["event_type"] == "view"),
            "carts_1h": sum(1 for row in h1 if row["event_type"] == "cart"),
            "carts_24h": sum(1 for row in h24 if row["event_type"] == "cart"),
            "purchases_24h": sum(1 for row in h24 if row["event_type"] == "purchase"),
            "purchases_7d": purchases_7d,
            "conversion_rate_7d": float((purchases_7d + 1.0) / (views_7d + 10.0)),
            "popularity_score": float(popularity),
            "updated_at": isoformat_utc(ts),
            "feature_version": "item_features_v1",
        }
