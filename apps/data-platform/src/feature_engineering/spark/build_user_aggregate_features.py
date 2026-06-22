from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd


def _count_events(frame: pd.DataFrame, start_ts: pd.Timestamp, end_ts: pd.Timestamp, event_type: str) -> int:
    mask = (
        (frame["event_timestamp"] > start_ts)
        & (frame["event_timestamp"] <= end_ts)
        & (frame["event_type"] == event_type)
    )
    return int(mask.sum())


def build_user_aggregate_features(
    clean_events: pd.DataFrame,
    feature_version: str = "user_aggregate_v1",
) -> pd.DataFrame:
    if clean_events.empty:
        return pd.DataFrame()
    events = clean_events.copy()
    events["event_timestamp"] = pd.to_datetime(events["event_timestamp"], utc=True)
    rows: list[dict] = []
    for user_id, group in events.sort_values("event_timestamp").groupby("user_id"):
        group = group.reset_index(drop=True)
        for _, event in group.iterrows():
            ts = event["event_timestamp"]
            w30m = ts - pd.Timedelta(minutes=30)
            w24h = ts - pd.Timedelta(hours=24)
            w7d = ts - pd.Timedelta(days=7)
            recent_7d = group[(group["event_timestamp"] > w7d) & (group["event_timestamp"] <= ts)]
            carts_7d = int((recent_7d["event_type"] == "cart").sum())
            purchases_7d = int((recent_7d["event_type"] == "purchase").sum())
            rows.append(
                {
                    "user_id": int(user_id),
                    "feature_timestamp": ts,
                    "event_timestamp": ts,
                    "views_30m": _count_events(group, w30m, ts, "view"),
                    "carts_30m": _count_events(group, w30m, ts, "cart"),
                    "purchases_24h": _count_events(group, w24h, ts, "purchase"),
                    "distinct_categories_7d": int(recent_7d["category_id"].nunique()),
                    "avg_viewed_price_7d": float(
                        recent_7d.loc[recent_7d["event_type"] == "view", "price"].astype(float).mean()
                        if not recent_7d.empty
                        else 0.0
                    ),
                    "cart_to_purchase_ratio_7d": float(purchases_7d / carts_7d) if carts_7d else 0.0,
                    "last_event_age_seconds": 0,
                    "aggregation_window_end_ts": ts,
                    "watermark_ts": ts,
                    "created_timestamp": datetime.now(timezone.utc),
                    "feature_version": feature_version,
                }
            )
    return pd.DataFrame(rows).fillna({"avg_viewed_price_7d": 0.0})

