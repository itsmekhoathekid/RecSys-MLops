from __future__ import annotations

from pathlib import Path

import pandas as pd

from ingest.minio_raw_reader import read_generator_run
from preprocess.event_dedup import deduplicate_behavior_events
from preprocess.schema_evolution import (
    normalize_behavior_schema,
    normalize_recommendation_schema,
)
from feature_store.offline_writer import write_feature_table


def build_clean_behavior_events(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    dedup = deduplicate_behavior_events(events)
    clean = normalize_behavior_schema(dedup.clean)
    clean["event_timestamp"] = pd.to_datetime(clean["event_timestamp"], utc=True)
    clean["ingestion_ts"] = pd.to_datetime(clean["ingestion_ts"], utc=True)
    clean["event_type_id"] = clean["event_type"].map({"view": 1, "cart": 2, "purchase": 3}).fillna(0).astype("int16")
    return clean.sort_values(["event_timestamp", "event_id"]).reset_index(drop=True), dedup.rejected


def build_clean_impressions(impressions: pd.DataFrame) -> pd.DataFrame:
    frame = impressions.copy()
    frame["impression_timestamp"] = pd.to_datetime(frame["impression_timestamp"], utc=True)
    return frame.drop_duplicates("impression_id").sort_values(
        ["impression_timestamp", "impression_id"]
    ).reset_index(drop=True)


def build_order_facts(orders: pd.DataFrame, order_items: pd.DataFrame) -> pd.DataFrame:
    facts = order_items.merge(orders, on="order_id", how="left", suffixes=("_item", "_order"))
    if "status" in facts.columns:
        facts["is_valid_purchase"] = ~facts["status"].isin(["cancelled", "refunded"])
    else:
        facts["is_valid_purchase"] = True
    return facts


def build_product_scd(product_snapshots: pd.DataFrame, products: pd.DataFrame) -> pd.DataFrame:
    if not product_snapshots.empty:
        frame = product_snapshots.copy()
        frame["valid_from"] = pd.to_datetime(frame["valid_from"], utc=True)
        return frame.sort_values(["product_id", "valid_from"]).reset_index(drop=True)
    frame = products.copy()
    frame["valid_from"] = pd.to_datetime(frame["created_ts"], utc=True)
    frame["valid_to"] = pd.NaT
    return frame


def build_silver_tables(run_path: str | Path, output_path: str | Path) -> dict[str, pd.DataFrame]:
    raw = read_generator_run(run_path)
    clean_events, rejected_events = build_clean_behavior_events(raw["behavior_events"])
    clean_requests = normalize_recommendation_schema(raw["recommendation_requests"])
    silver = {
        "clean_behavior_events": clean_events,
        "rejected_behavior_events": rejected_events,
        "clean_impressions": build_clean_impressions(raw["impressions"]),
        "clean_recommendation_requests": clean_requests,
        "order_facts": build_order_facts(raw["orders"], raw["order_items"]),
        "product_scd": build_product_scd(raw["product_snapshots"], raw["products"]),
        "users": raw["users"],
        "products": raw["products"],
        "user_preferences": raw["user_preferences"],
    }
    base = Path(output_path)
    for name, frame in silver.items():
        write_feature_table(frame, base / name)
    return silver

