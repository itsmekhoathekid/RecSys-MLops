from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from features.flink.event_time import late_arrival_metrics
from features.flink.pyflink_compat import MapFunction
from features.flink.time_utils import isoformat_utc, parse_event_time


def flink_timestamp(value: Any) -> datetime:
    dt = parse_event_time(value) if isinstance(value, str) else value
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    microsecond = (dt.microsecond // 1000) * 1000
    return dt.replace(microsecond=microsecond)


def build_stream_behavior_row(
    event: dict[str, Any],
    source_topic: str,
    allowed_lateness_seconds: int,
) -> dict[str, Any]:
    late_by_seconds, is_late = late_arrival_metrics(event, allowed_lateness_seconds)
    feature_ts = parse_event_time(event["event_timestamp"])
    return {
        "event_id": str(event["event_id"]),
        "event_timestamp": feature_ts,
        "processed_timestamp": datetime.now(timezone.utc),
        "user_id": int(event["user_id"]),
        "product_id": int(event["product_id"]),
        "event_type": str(event["event_type"]),
        "event_type_id": int(event["event_type_id"]),
        "category_id": int(event["category_id"]),
        "brand_id": int(event["brand_id"]),
        "price": float(event["price"]),
        "price_bucket": int(event["price_bucket"]),
        "payload_hash": str(event.get("payload_hash") or ""),
        "source_topic": source_topic,
        "late_by_seconds": late_by_seconds,
        "is_late": is_late,
    }


def build_offline_user_feature_rows(
    update: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    event = update["event"]
    feature_ts = parse_event_time(event["event_timestamp"])
    sequence_payload = update["sequence_payload"]
    aggregate_payload = update["aggregate_payload"]
    return {
        "stream_user_sequence_features": [
            {
                "source_event_id": str(event["event_id"]),
                "user_id": int(sequence_payload["user_id"]),
                "feature_timestamp": feature_ts,
                "sequence_length": int(sequence_payload["sequence_length"]),
                "max_history_length": int(sequence_payload["max_history_length"]),
                "feature_payload": sequence_payload,
                "feature_version": sequence_payload["feature_version"],
            }
        ],
        "stream_user_aggregate_features": [
            {
                "source_event_id": str(event["event_id"]),
                "user_id": int(aggregate_payload["user_id"]),
                "feature_timestamp": feature_ts,
                "views_30m": int(aggregate_payload["views_30m"]),
                "carts_30m": int(aggregate_payload["carts_30m"]),
                "purchases_24h": int(aggregate_payload["purchases_24h"]),
                "feature_payload": aggregate_payload,
                "feature_version": aggregate_payload["feature_version"],
            }
        ],
    }


def build_offline_item_feature_rows(
    update: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    event = update["event"]
    feature_ts = parse_event_time(event["event_timestamp"])
    item_payload = update["item_payload"]
    return {
        "stream_item_features": [
            {
                "source_event_id": str(event["event_id"]),
                "product_id": int(item_payload["product_id"]),
                "feature_timestamp": feature_ts,
                "category_id": int(item_payload["category_id"]),
                "brand_id": int(item_payload["brand_id"]),
                "price_bucket": int(item_payload["price_bucket"]),
                "views_1h": int(item_payload["views_1h"]),
                "views_24h": int(item_payload["views_24h"]),
                "purchases_24h": int(item_payload["purchases_24h"]),
                "popularity_score": float(item_payload["popularity_score"]),
                "feature_payload": item_payload,
                "feature_version": item_payload["feature_version"],
            }
        ],
    }


def _event_time_pair(event: dict[str, Any]) -> tuple[datetime, str]:
    feature_ts = parse_event_time(event["event_timestamp"])
    return feature_ts, isoformat_utc(feature_ts)


def build_postgres_user_feature_rows(
    update: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    event = update["event"]
    sequence_payload = update["sequence_payload"]
    aggregate_payload = update["aggregate_payload"]
    feature_ts, feature_ts_text = _event_time_pair(event)
    created_ts = datetime.now(timezone.utc)
    return {
        "user_sequence_features": [
            {
                "source_event_id": str(event["event_id"]),
                "user_id": int(sequence_payload["user_id"]),
                "feature_timestamp": feature_ts,
                "event_timestamp": feature_ts,
                "created_timestamp": created_ts,
                "hist_item_ids": [int(value) for value in sequence_payload["item_ids"]],
                "hist_event_type_ids": [
                    int(value) for value in sequence_payload["event_type_ids"]
                ],
                "hist_category_ids": [
                    int(value) for value in sequence_payload["category_ids"]
                ],
                "hist_brand_ids": [
                    int(value) for value in sequence_payload["brand_ids"]
                ],
                "hist_price_bucket_ids": [
                    int(value) for value in sequence_payload["price_bucket_ids"]
                ],
                "hist_event_timestamps": [
                    str(value) for value in sequence_payload["event_timestamps"]
                ],
                "hist_request_ids": [
                    str(value) for value in sequence_payload["request_ids"]
                ],
                "hist_impression_ids": [
                    str(value) for value in sequence_payload["impression_ids"]
                ],
                "hist_length": int(sequence_payload["sequence_length"]),
                "max_history_length": int(sequence_payload["max_history_length"]),
                "feature_version": str(sequence_payload["feature_version"]),
            }
        ],
        "user_aggregate_features": [
            {
                "source_event_id": str(event["event_id"]),
                "user_id": int(aggregate_payload["user_id"]),
                "feature_timestamp": feature_ts,
                "event_timestamp": feature_ts,
                "views_30m": int(aggregate_payload["views_30m"]),
                "carts_30m": int(aggregate_payload["carts_30m"]),
                "purchases_24h": int(aggregate_payload["purchases_24h"]),
                "distinct_categories_7d": int(
                    aggregate_payload["distinct_categories_7d"]
                ),
                "avg_viewed_price_7d": float(aggregate_payload["avg_viewed_price_7d"]),
                "cart_to_purchase_ratio_7d": float(
                    aggregate_payload["cart_to_purchase_ratio_7d"]
                ),
                "last_event_age_seconds": int(
                    aggregate_payload["last_event_age_seconds"]
                ),
                "aggregation_window_end_ts": aggregate_payload.get(
                    "updated_at", feature_ts_text
                ),
                "watermark_ts": feature_ts,
                "created_timestamp": created_ts,
                "feature_version": str(aggregate_payload["feature_version"]),
            }
        ],
    }


def build_postgres_item_feature_rows(
    update: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    event = update["event"]
    item_payload = update["item_payload"]
    feature_ts, feature_ts_text = _event_time_pair(event)
    created_ts = datetime.now(timezone.utc)
    return {
        "item_features": [
            {
                "source_event_id": str(event["event_id"]),
                "product_id": int(item_payload["product_id"]),
                "feature_timestamp": feature_ts,
                "event_timestamp": feature_ts,
                "category_id": int(item_payload["category_id"]),
                "brand_id": int(item_payload["brand_id"]),
                "price_bucket": int(item_payload["price_bucket"]),
                "is_active": bool(item_payload["is_active"]),
                "views_1h": int(item_payload["views_1h"]),
                "views_24h": int(item_payload["views_24h"]),
                "carts_1h": int(item_payload["carts_1h"]),
                "carts_24h": int(item_payload["carts_24h"]),
                "purchases_24h": int(item_payload["purchases_24h"]),
                "purchases_7d": int(item_payload["purchases_7d"]),
                "conversion_rate_7d": float(item_payload["conversion_rate_7d"]),
                "popularity_score": float(item_payload["popularity_score"]),
                "aggregation_window_end_ts": item_payload.get(
                    "updated_at", feature_ts_text
                ),
                "watermark_ts": feature_ts,
                "created_timestamp": created_ts,
                "feature_version": str(item_payload["feature_version"]),
            }
        ],
    }


def build_late_event_dlq_row(
    event: dict[str, Any],
    source_topic: str,
    allowed_lateness_seconds: int,
    reason: str = "too_late_for_feature_update",
) -> dict[str, Any]:
    late_by_seconds, _ = late_arrival_metrics(event, allowed_lateness_seconds)
    created_ts = datetime.now(timezone.utc)
    event_ts = parse_event_time(event["event_timestamp"])
    return {
        "event_id": str(event["event_id"]),
        "user_id": int(event["user_id"]),
        "product_id": int(event["product_id"]),
        "event_type": str(event["event_type"]),
        "event_timestamp": event_ts,
        "processed_timestamp": created_ts,
        "late_by_seconds": late_by_seconds,
        "allowed_lateness_seconds": int(allowed_lateness_seconds),
        "source_topic": source_topic,
        "payload_hash": str(event.get("payload_hash") or ""),
        "reason": reason,
        "payload": json.dumps(event, default=str, sort_keys=True),
        "created_timestamp": created_ts,
    }


class QualityWindowMetricLog(MapFunction):
    def __init__(self, args: Any) -> None:
        self.args = args

    def map(self, row: dict[str, Any]) -> str:
        return json.dumps(
            {
                "status": "streaming_quality_window_metrics",
                "window_start": isoformat_utc(row["window_start"]),
                "window_end": isoformat_utc(row["window_end"]),
                "topic": row["topic"],
                "event_count": int(row["event_count"]),
                "late_event_count": int(row["late_event_count"]),
                "late_events_dropped": int(row["late_events_dropped"]),
                "side_output_late_events": int(row["side_output_late_events"]),
                "duplicate_event_count": int(row["duplicate_event_count"]),
                "max_late_by_seconds": float(row["max_late_by_seconds"]),
                "is_bursty": bool(row["is_bursty"]),
                "drop_late_events": bool(self.args.drop_late_events),
            },
            sort_keys=True,
        )


def quality_window_metric_log_operator(args: Any) -> QualityWindowMetricLog:
    return QualityWindowMetricLog(args)
