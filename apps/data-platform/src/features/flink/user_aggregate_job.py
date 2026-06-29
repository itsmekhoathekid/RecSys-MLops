from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from features.flink.time_utils import isoformat_utc, parse_event_time


@dataclass
class UserAggregateState:
    events_by_user: dict[int, deque[dict[str, Any]]] = field(default_factory=lambda: defaultdict(deque))

    def update(self, event: dict[str, Any]) -> dict[str, Any]:
        user_id = int(event["user_id"])
        ts = parse_event_time(event["event_timestamp"])
        history = self.events_by_user[user_id]
        history.append(event)
        while history and parse_event_time(history[0]["event_timestamp"]) < ts - timedelta(days=7):
            history.popleft()
        rows = list(history)
        h30 = [row for row in rows if parse_event_time(row["event_timestamp"]) > ts - timedelta(minutes=30)]
        h24 = [row for row in rows if parse_event_time(row["event_timestamp"]) > ts - timedelta(hours=24)]
        viewed_prices = [float(row.get("price", 0.0) or 0.0) for row in rows if row["event_type"] == "view"]
        carts_7d = sum(1 for row in rows if row["event_type"] == "cart")
        purchases_7d = sum(1 for row in rows if row["event_type"] == "purchase")
        return {
            "user_id": user_id,
            "views_30m": sum(1 for row in h30 if row["event_type"] == "view"),
            "carts_30m": sum(1 for row in h30 if row["event_type"] == "cart"),
            "purchases_24h": sum(1 for row in h24 if row["event_type"] == "purchase"),
            "distinct_categories_7d": len({int(row["category_id"]) for row in rows}),
            "avg_viewed_price_7d": float(sum(viewed_prices) / len(viewed_prices)) if viewed_prices else 0.0,
            "cart_to_purchase_ratio_7d": float(purchases_7d / carts_7d) if carts_7d else 0.0,
            "last_event_age_seconds": 0,
            "updated_at": isoformat_utc(ts),
            "feature_version": "user_aggregate_v1",
        }
