from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd


def s3_options() -> dict[str, Any]:
    return {
        "key": os.getenv("MINIO_ROOT_USER", "minio"),
        "secret": os.getenv("MINIO_ROOT_PASSWORD", "minio123"),
        "client_kwargs": {
            "endpoint_url": os.getenv("MINIO_ENDPOINT", "http://minio:9000"),
            "region_name": "us-east-1",
        },
    }


def list_json_files(path: str | Path) -> list[str]:
    raw = str(path).rstrip("/")
    if raw.startswith("s3://"):
        import s3fs

        parsed = urlparse(raw)
        root = f"{parsed.netloc}{parsed.path}".rstrip("/")
        filesystem = s3fs.S3FileSystem(anon=False, **s3_options())
        return sorted(
            f"s3://{file}" for file in filesystem.find(root)
            if file.endswith(".json") or ".json" in Path(file).name
        )
    root = Path(raw)
    return [str(file) for file in sorted(root.rglob("*.json"))]


def iter_json_records(path: str | Path):
    raw = str(path)
    if raw.startswith("s3://"):
        import s3fs

        filesystem = s3fs.S3FileSystem(anon=False, **s3_options())
        file_path = raw.removeprefix("s3://")
        with filesystem.open(file_path, "r") as file:
            yield from _iter_json_lines(file)
        return
    with Path(raw).open("r", encoding="utf-8") as file:
        yield from _iter_json_lines(file)


def _iter_json_lines(file):
    for line in file:
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if isinstance(payload, list):
            yield from payload
        else:
            yield payload


def extract_debezium_after(record: dict[str, Any]) -> dict[str, Any] | None:
    payload = record.get("payload", record)
    op = payload.get("op")
    if op in {"d", "t"}:
        return None
    after = payload.get("after")
    if after is None and "schema" in record and "payload" in record:
        return None
    if after is None:
        after = payload
    return after


def read_bronze_cdc_table(bronze_root: str | Path, topic: str) -> pd.DataFrame:
    bronze_root_text = str(bronze_root).rstrip("/")
    topic_roots = [
        f"{bronze_root_text}/{topic}",
        f"{bronze_root_text}/topic={topic}",
    ]
    rows: list[dict[str, Any]] = []
    for topic_root in topic_roots:
        for file in list_json_files(topic_root):
            for record in iter_json_records(file):
                after = extract_debezium_after(record)
                if after:
                    rows.append(after)
    return pd.DataFrame(rows)


def normalize_behavior_events_from_cdc(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    events = frame.copy()
    timestamp_columns = ["event_timestamp", "created_ts", "ingestion_ts"]
    for column in timestamp_columns:
        if column in events.columns:
            events[column] = pd.to_datetime(events[column], utc=True)
    if "event_date" in events.columns:
        events["event_date"] = pd.to_datetime(events["event_date"]).dt.date
    if "event_type_id" not in events.columns and "event_type" in events.columns:
        events["event_type_id"] = (
            events["event_type"].map({"view": 1, "cart": 2, "purchase": 3}).fillna(0).astype("int16")
        )
    for column in ["user_id", "product_id", "category_id", "brand_id", "price_bucket"]:
        if column in events.columns:
            events[column] = pd.to_numeric(events[column], errors="coerce").fillna(0)
    if "price" in events.columns:
        price = pd.to_numeric(events["price"], errors="coerce")
        if "price_bucket" in events.columns:
            price = price.fillna(pd.to_numeric(events["price_bucket"], errors="coerce"))
        events["price"] = price.fillna(0.0)
    return events.sort_values(["event_timestamp", "event_id"]).reset_index(drop=True)
