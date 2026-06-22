from __future__ import annotations

from datetime import datetime
from typing import Iterable

import pandas as pd


TIME_BUCKETS_SECONDS = (
    (1, 5 * 60),
    (2, 30 * 60),
    (3, 2 * 3600),
    (4, 6 * 3600),
    (5, 24 * 3600),
    (6, 3 * 24 * 3600),
    (7, 7 * 24 * 3600),
    (8, 14 * 24 * 3600),
)


def get_time_bucket(prediction_ts: datetime, hist_event_ts: datetime) -> int:
    delta_seconds = (prediction_ts - hist_event_ts).total_seconds()
    if delta_seconds < 0:
        return 0
    for bucket_id, upper_bound in TIME_BUCKETS_SECONDS:
        if delta_seconds < upper_bound:
            return bucket_id
    return 9


def get_time_buckets(
    prediction_ts: datetime, hist_event_timestamps: Iterable[datetime]
) -> list[int]:
    return [get_time_bucket(prediction_ts, ts) for ts in hist_event_timestamps]


def latest_asof(
    features: pd.DataFrame,
    entity_column: str,
    timestamp_column: str,
    entity_value: object,
    asof_ts: object,
) -> pd.Series | None:
    if features.empty:
        return None
    frame = features[features[entity_column] == entity_value].copy()
    if frame.empty:
        return None
    frame[timestamp_column] = pd.to_datetime(frame[timestamp_column], utc=True)
    asof = pd.Timestamp(asof_ts)
    if asof.tzinfo is None:
        asof = asof.tz_localize("UTC")
    candidates = frame[frame[timestamp_column] <= asof]
    if candidates.empty:
        return None
    return candidates.sort_values(timestamp_column).iloc[-1]

