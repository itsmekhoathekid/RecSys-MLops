from __future__ import annotations

import pandas as pd


def normalize_behavior_schema(events: pd.DataFrame) -> pd.DataFrame:
    frame = events.copy()
    for column, default in {
        "device_type": "unknown",
        "campaign_id": "none",
        "page_context": "unknown",
    }.items():
        if column not in frame.columns:
            frame[column] = default
        frame[column] = frame[column].fillna(default)
    return frame


def normalize_recommendation_schema(requests: pd.DataFrame) -> pd.DataFrame:
    frame = requests.copy()
    for column, default in {
        "device_type": "unknown",
        "campaign_id": "none",
        "context_product_id": 0,
        "context_category_id": 0,
    }.items():
        if column not in frame.columns:
            frame[column] = default
        frame[column] = frame[column].fillna(default)
    return frame

