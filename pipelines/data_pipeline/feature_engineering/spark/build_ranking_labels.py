from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd


def build_ranking_labels(
    impressions: pd.DataFrame,
    clean_events: pd.DataFrame,
    label_window_hours: int = 24,
    label_version: str = "ranking_label_v1",
) -> pd.DataFrame:
    if impressions.empty:
        return pd.DataFrame()
    imps = impressions.copy()
    events = clean_events.copy()
    imps["impression_timestamp"] = pd.to_datetime(imps["impression_timestamp"], utc=True)
    events["event_timestamp"] = pd.to_datetime(events["event_timestamp"], utc=True)
    positive_events = events[events["event_type"].isin(["cart", "purchase"])].copy()
    rows: list[dict] = []
    for _, impression in imps.iterrows():
        prediction_ts = impression["impression_timestamp"]
        label_window_end = prediction_ts + pd.Timedelta(hours=label_window_hours)
        matches = positive_events[
            (positive_events["user_id"] == impression["user_id"])
            & (positive_events["product_id"] == impression["candidate_product_id"])
            & (positive_events["event_timestamp"] > prediction_ts)
            & (positive_events["event_timestamp"] <= label_window_end)
        ].sort_values(["event_timestamp", "event_type"])
        positive = not matches.empty
        first_positive = matches.iloc[0] if positive else None
        rows.append(
            {
                "impression_id": str(impression["impression_id"]),
                "request_id": str(impression["request_id"]),
                "user_id": int(impression["user_id"]),
                "candidate_product_id": int(impression["candidate_product_id"]),
                "prediction_timestamp": prediction_ts,
                "label_window_end": label_window_end,
                "label": 1 if positive else 0,
                "positive_event_type": first_positive["event_type"] if positive else None,
                "positive_event_timestamp": first_positive["event_timestamp"] if positive else None,
                "sampling_strategy": "impression",
                "sampling_probability": 1.0,
                "candidate_source": impression.get("candidate_source", "unknown"),
                "rank_position": int(impression["rank_position"]) if pd.notna(impression.get("rank_position")) else None,
                "created_timestamp": datetime.now(timezone.utc),
                "label_version": label_version,
            }
        )
    return pd.DataFrame(rows)

