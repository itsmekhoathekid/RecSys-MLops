from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from feature_store.offline_writer import read_feature_table
from feature_store.online_writer import RedisOnlineWriter, dumps_feature_payload


FEATURE_VIEWS = {
    "user_sequence_features": {
        "entity_column": "user_id",
        "timestamp_column": "event_timestamp",
        "writer_method": "write_user_sequence",
        "ttl_key": "user_sequence",
    },
    "user_aggregate_features": {
        "entity_column": "user_id",
        "timestamp_column": "event_timestamp",
        "writer_method": "write_user_aggregate",
        "ttl_key": "user_aggregate",
    },
    "item_features": {
        "entity_column": "product_id",
        "timestamp_column": "event_timestamp",
        "writer_method": "write_item_features",
        "ttl_key": "item_features",
    },
}

DEFAULT_TTLS = {
    "user_sequence": 90 * 24 * 60 * 60,
    "user_aggregate": 24 * 60 * 60,
    "item_features": 7 * 24 * 60 * 60,
}


@dataclass(frozen=True)
class SyncResult:
    feature_view: str
    scanned_rows: int
    synced_rows: int
    skipped_rows: int


def load_ttls(path: str | Path = "configs/local/redis_online_store.yaml") -> dict[str, int]:
    config_path = Path(path)
    if not config_path.exists():
        return DEFAULT_TTLS
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    configured = config.get("ttl_seconds", {})
    return {**DEFAULT_TTLS, **{key: int(value) for key, value in configured.items()}}


def latest_by_entity(frame: pd.DataFrame, entity_column: str, timestamp_column: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    if entity_column not in frame.columns:
        raise ValueError(f"offline feature table missing entity column {entity_column}")
    if timestamp_column not in frame.columns:
        raise ValueError(f"offline feature table missing timestamp column {timestamp_column}")
    normalized = frame.copy()
    normalized[timestamp_column] = pd.to_datetime(normalized[timestamp_column], utc=True)
    return (
        normalized.sort_values([entity_column, timestamp_column])
        .drop_duplicates(entity_column, keep="last")
        .reset_index(drop=True)
    )


def row_payload(row: pd.Series) -> dict[str, Any]:
    payload = row.to_dict()
    return {key: _jsonable(value) for key, value in payload.items()}


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if not isinstance(value, (str, bytes)) and hasattr(value, "tolist"):
        return _jsonable(value.tolist())
    if hasattr(value, "isoformat"):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return value
    return value


def payload_timestamp(payload: dict[str, Any], timestamp_column: str = "event_timestamp") -> pd.Timestamp | None:
    value = payload.get(timestamp_column) or payload.get("updated_at") or payload.get("feature_timestamp")
    if value in {None, ""}:
        return None
    try:
        return pd.Timestamp(value).tz_convert("UTC") if pd.Timestamp(value).tzinfo else pd.Timestamp(value, tz="UTC")
    except Exception:
        return None


def redis_payload(redis_client: Any, key: str) -> dict[str, Any] | None:
    raw = redis_client.get(key)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def should_overwrite(existing: dict[str, Any] | None, incoming: dict[str, Any], timestamp_column: str) -> bool:
    if existing is None:
        return True
    existing_ts = payload_timestamp(existing, timestamp_column)
    incoming_ts = payload_timestamp(incoming, timestamp_column)
    if existing_ts is None:
        return True
    if incoming_ts is None:
        return False
    return incoming_ts >= existing_ts


def redis_key_for(writer: RedisOnlineWriter, feature_view: str, entity_id: int) -> str:
    if feature_view == "user_sequence_features":
        return writer.keys.user_sequence.format(user_id=entity_id)
    if feature_view == "user_aggregate_features":
        return writer.keys.user_aggregate.format(user_id=entity_id)
    if feature_view == "item_features":
        return writer.keys.item_features.format(product_id=entity_id)
    raise ValueError(f"Unsupported feature view: {feature_view}")


def write_payload(
    writer: RedisOnlineWriter,
    feature_view: str,
    entity_id: int,
    payload: dict[str, Any],
    ttl_seconds: int,
) -> str:
    if feature_view == "user_sequence_features":
        return writer.write_user_sequence(entity_id, payload, ttl_seconds)
    if feature_view == "user_aggregate_features":
        return writer.write_user_aggregate(entity_id, payload, ttl_seconds)
    if feature_view == "item_features":
        return writer.write_item_features(entity_id, payload, ttl_seconds)
    raise ValueError(f"Unsupported feature view: {feature_view}")


def sync_feature_view(
    offline_root: str | Path,
    feature_view: str,
    writer: RedisOnlineWriter,
    ttl_seconds: int,
) -> SyncResult:
    contract = FEATURE_VIEWS[feature_view]
    table_path = str(offline_root).rstrip("/") + "/" + feature_view
    frame = read_feature_table(table_path)
    latest = latest_by_entity(
        frame,
        contract["entity_column"],
        contract["timestamp_column"],
    )
    synced = 0
    skipped = 0
    for _, row in latest.iterrows():
        entity_id = int(row[contract["entity_column"]])
        payload = row_payload(row)
        key = redis_key_for(writer, feature_view, entity_id)
        existing = redis_payload(writer.redis_client, key)
        if not should_overwrite(existing, payload, contract["timestamp_column"]):
            skipped += 1
            continue
        write_payload(writer, feature_view, entity_id, payload, ttl_seconds)
        synced += 1
    return SyncResult(
        feature_view=feature_view,
        scanned_rows=len(frame),
        synced_rows=synced,
        skipped_rows=skipped,
    )


def write_monitoring_rows(run_id: str, results: list[SyncResult]) -> None:
    try:
        from warehouse.connection import connect
        from warehouse.schemas import MONITORING_ONLINE_STORE_SYNC_RUNS
        from warehouse.writer import upsert_rows
    except Exception:
        return
    rows = [
        {
            "run_id": run_id,
            "feature_view": result.feature_view,
            "scanned_rows": result.scanned_rows,
            "synced_rows": result.synced_rows,
            "skipped_rows": result.skipped_rows,
            "created_timestamp": datetime.now(timezone.utc),
        }
        for result in results
    ]
    with connect() as connection:
        upsert_rows(connection, MONITORING_ONLINE_STORE_SYNC_RUNS, rows)


def sync_offline_to_online(
    offline_root: str | Path,
    redis_client: Any,
    ttl_config_path: str | Path = "configs/local/redis_online_store.yaml",
    run_id: str | None = None,
    write_monitoring: bool = True,
) -> dict[str, dict[str, int]]:
    writer = RedisOnlineWriter(redis_client)
    ttls = load_ttls(ttl_config_path)
    results = [
        sync_feature_view(
            offline_root,
            feature_view,
            writer,
            ttl_seconds=ttls[contract["ttl_key"]],
        )
        for feature_view, contract in FEATURE_VIEWS.items()
    ]
    if write_monitoring and run_id is not None:
        write_monitoring_rows(run_id, results)
    return {
        result.feature_view: {
            "scanned_rows": result.scanned_rows,
            "synced_rows": result.synced_rows,
            "skipped_rows": result.skipped_rows,
        }
        for result in results
    }


def main() -> int:
    import redis

    parser = argparse.ArgumentParser(description="Sync Feast offline feature parquet into serving Redis keys.")
    parser.add_argument("--offline-root", default=os.getenv("FEAST_OFFLINE_ROOT", "s3://recsys-feature-store/offline"))
    parser.add_argument("--ttl-config", default="configs/local/redis_online_store.yaml")
    parser.add_argument("--run-id", default=os.getenv("ONLINE_STORE_SYNC_RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")))
    parser.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "redis"))
    parser.add_argument("--redis-port", type=int, default=int(os.getenv("REDIS_PORT", "6379")))
    parser.add_argument("--redis-db", type=int, default=int(os.getenv("REDIS_DB", "0")))
    parser.add_argument("--skip-monitoring", action="store_true")
    args = parser.parse_args()

    client = redis.Redis(host=args.redis_host, port=args.redis_port, db=args.redis_db, decode_responses=True)
    result = sync_offline_to_online(
        args.offline_root,
        client,
        ttl_config_path=args.ttl_config,
        run_id=args.run_id,
        write_monitoring=not args.skip_monitoring,
    )
    print(json.dumps({"run_id": args.run_id, "synced": result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
