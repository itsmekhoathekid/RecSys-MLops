from __future__ import annotations

import pandas as pd

from preprocess.point_in_time import (
    get_time_buckets,
    latest_asof,
)


def _empty_sequence(max_history_length: int) -> dict:
    return {
        "hist_item_ids": [],
        "hist_event_type_ids": [],
        "hist_category_ids": [],
        "hist_brand_ids": [],
        "hist_price_bucket_ids": [],
        "hist_event_timestamps": [],
        "hist_length": 0,
        "max_history_length": max_history_length,
    }


def build_bst_training_table(
    labels: pd.DataFrame,
    user_sequence_features: pd.DataFrame,
    user_aggregate_features: pd.DataFrame,
    item_features: pd.DataFrame,
    max_history_length: int = 50,
) -> pd.DataFrame:
    if labels.empty:
        return pd.DataFrame()
    rows: list[dict] = []
    for _, label in labels.iterrows():
        prediction_ts = pd.Timestamp(label["prediction_timestamp"])
        sequence = latest_asof(
            user_sequence_features,
            "user_id",
            "feature_timestamp",
            label["user_id"],
            prediction_ts,
        )
        aggregate = latest_asof(
            user_aggregate_features,
            "user_id",
            "feature_timestamp",
            label["user_id"],
            prediction_ts,
        )
        item = latest_asof(
            item_features,
            "product_id",
            "feature_timestamp",
            label["candidate_product_id"],
            prediction_ts,
        )
        seq = sequence.to_dict() if sequence is not None else _empty_sequence(max_history_length)
        item_dict = item.to_dict() if item is not None else {}
        hist_ts = [
            pd.Timestamp(ts).to_pydatetime()
            for ts in seq.get("hist_event_timestamps", [])
            if pd.Timestamp(ts) < prediction_ts
        ]
        hist_len = len(hist_ts)
        rows.append(
            {
                "impression_id": label["impression_id"],
                "request_id": label["request_id"],
                "user_id": int(label["user_id"]),
                "hist_item_id": list(seq.get("hist_item_ids", []))[-hist_len:],
                "hist_event_type": list(seq.get("hist_event_type_ids", []))[-hist_len:],
                "hist_category": list(seq.get("hist_category_ids", []))[-hist_len:],
                "hist_brand": list(seq.get("hist_brand_ids", []))[-hist_len:],
                "hist_price_bucket": list(seq.get("hist_price_bucket_ids", []))[-hist_len:],
                "hist_time": get_time_buckets(prediction_ts.to_pydatetime(), hist_ts),
                "target_item_id": int(label["candidate_product_id"]),
                "target_category": int(item_dict.get("category_id", 0) or 0),
                "target_brand": int(item_dict.get("brand_id", 0) or 0),
                "target_price_bucket": int(item_dict.get("price_bucket", 0) or 0),
                "event_time": int(prediction_ts.timestamp()),
                "prediction_timestamp": prediction_ts,
                "label": int(label["label"]),
                "views_30m": int(aggregate.get("views_30m", 0)) if aggregate is not None else 0,
                "carts_30m": int(aggregate.get("carts_30m", 0)) if aggregate is not None else 0,
                "purchases_24h": int(aggregate.get("purchases_24h", 0)) if aggregate is not None else 0,
            }
        )
    return pd.DataFrame(rows)

