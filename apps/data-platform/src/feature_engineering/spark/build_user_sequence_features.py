from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd


SEQUENCE_COLUMNS = [
    "hist_item_ids",
    "hist_event_type_ids",
    "hist_category_ids",
    "hist_brand_ids",
    "hist_price_bucket_ids",
    "hist_event_timestamps",
    "hist_request_ids",
    "hist_impression_ids",
]


def build_user_sequence_features(
    clean_events: pd.DataFrame,
    max_history_length: int = 50,
    feature_version: str = "bst_sequence_v2",
) -> pd.DataFrame:
    if clean_events.empty:
        return pd.DataFrame()
    events = clean_events.copy()
    events["event_timestamp"] = pd.to_datetime(events["event_timestamp"], utc=True)
    rows: list[dict] = []
    for user_id, group in events.sort_values("event_timestamp").groupby("user_id"):
        history: list[dict] = []
        for _, event in group.iterrows():
            history.append(event.to_dict())
            window = history[-max_history_length:]
            rows.append(
                {
                    "user_id": int(user_id),
                    "feature_timestamp": event["event_timestamp"],
                    "event_timestamp": event["event_timestamp"],
                    "created_timestamp": datetime.now(timezone.utc),
                    "hist_item_ids": [int(row["product_id"]) for row in window],
                    "hist_event_type_ids": [int(row["event_type_id"]) for row in window],
                    "hist_category_ids": [int(row["category_id"]) for row in window],
                    "hist_brand_ids": [int(row["brand_id"]) for row in window],
                    "hist_price_bucket_ids": [int(row["price_bucket"]) for row in window],
                    "hist_event_timestamps": [pd.Timestamp(row["event_timestamp"]).isoformat() for row in window],
                    "hist_request_ids": [str(row.get("request_id") or "") for row in window],
                    "hist_impression_ids": [str(row.get("impression_id") or "") for row in window],
                    "hist_length": len(window),
                    "max_history_length": max_history_length,
                    "feature_version": feature_version,
                }
            )
    return pd.DataFrame(rows)
