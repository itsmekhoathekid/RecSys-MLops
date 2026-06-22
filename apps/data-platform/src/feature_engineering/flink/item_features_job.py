from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pandas as pd


@dataclass
class ItemFeatureState:
    events_by_product: dict[int, deque[dict[str, Any]]] = field(default_factory=lambda: defaultdict(deque))

    def update(self, event: dict[str, Any]) -> dict[str, Any]:
        product_id = int(event["product_id"])
        ts = pd.Timestamp(event["event_timestamp"])
        history = self.events_by_product[product_id]
        history.append(event)
        while history and pd.Timestamp(history[0]["event_timestamp"]) < ts - timedelta(days=7):
            history.popleft()
        frame = pd.DataFrame(list(history))
        frame["event_timestamp"] = pd.to_datetime(frame["event_timestamp"], utc=True)
        h1 = frame[frame["event_timestamp"] > ts - pd.Timedelta(hours=1)]
        h24 = frame[frame["event_timestamp"] > ts - pd.Timedelta(hours=24)]
        views_7d = int((frame["event_type"] == "view").sum())
        purchases_7d = int((frame["event_type"] == "purchase").sum())
        popularity = (
            int((h24["event_type"] == "view").sum())
            + 3 * int((h24["event_type"] == "cart").sum())
            + 10 * int((h24["event_type"] == "purchase").sum())
        )
        return {
            "product_id": product_id,
            "category_id": int(event["category_id"]),
            "brand_id": int(event["brand_id"]),
            "price_bucket": int(event["price_bucket"]),
            "is_active": True,
            "views_1h": int((h1["event_type"] == "view").sum()),
            "views_24h": int((h24["event_type"] == "view").sum()),
            "carts_1h": int((h1["event_type"] == "cart").sum()),
            "carts_24h": int((h24["event_type"] == "cart").sum()),
            "purchases_24h": int((h24["event_type"] == "purchase").sum()),
            "purchases_7d": purchases_7d,
            "conversion_rate_7d": float((purchases_7d + 1.0) / (views_7d + 10.0)),
            "popularity_score": float(popularity),
            "updated_at": ts.isoformat(),
            "feature_version": "item_features_v1",
        }

