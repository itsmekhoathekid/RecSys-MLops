from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pandas as pd


@dataclass
class UserAggregateState:
    events_by_user: dict[int, deque[dict[str, Any]]] = field(default_factory=lambda: defaultdict(deque))

    def update(self, event: dict[str, Any]) -> dict[str, Any]:
        user_id = int(event["user_id"])
        ts = pd.Timestamp(event["event_timestamp"])
        history = self.events_by_user[user_id]
        history.append(event)
        while history and pd.Timestamp(history[0]["event_timestamp"]) < ts - timedelta(days=7):
            history.popleft()
        rows = list(history)
        frame = pd.DataFrame(rows)
        frame["event_timestamp"] = pd.to_datetime(frame["event_timestamp"], utc=True)
        w30m = ts - pd.Timedelta(minutes=30)
        w24h = ts - pd.Timedelta(hours=24)
        h30 = frame[frame["event_timestamp"] > w30m]
        h24 = frame[frame["event_timestamp"] > w24h]
        carts_7d = int((frame["event_type"] == "cart").sum())
        purchases_7d = int((frame["event_type"] == "purchase").sum())
        return {
            "user_id": user_id,
            "views_30m": int((h30["event_type"] == "view").sum()),
            "carts_30m": int((h30["event_type"] == "cart").sum()),
            "purchases_24h": int((h24["event_type"] == "purchase").sum()),
            "distinct_categories_7d": int(frame["category_id"].nunique()),
            "avg_viewed_price_7d": float(
                frame.loc[frame["event_type"] == "view", "price"].astype(float).mean()
                if not frame.empty
                else 0.0
            ),
            "cart_to_purchase_ratio_7d": float(purchases_7d / carts_7d) if carts_7d else 0.0,
            "last_event_age_seconds": 0,
            "updated_at": ts.isoformat(),
            "feature_version": "user_aggregate_v1",
        }

