from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from psycopg import sql

from feature_store.postgres_offline_store import PostgresOfflineStoreConfig
from recsys_feature_store_runtime.sql_registry_state import configure_registry_url


FEATURE_TABLES = (
    "user_sequence_features",
    "user_aggregate_features",
    "item_features",
)
DEFAULT_REPO_PATH = "/opt/recsys/apps/data-platform/feature-store/feature_repo"


@dataclass(frozen=True)
class SourceFeatureBounds:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class MaterializationResult:
    mode: str
    source_start: str
    source_end: str
    validation_status: str


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def source_feature_bounds(
    config: PostgresOfflineStoreConfig | None = None,
) -> SourceFeatureBounds:
    config = config or PostgresOfflineStoreConfig.from_env()
    minimum: datetime | None = None
    maximum: datetime | None = None
    with config.connect() as connection:
        with connection.cursor() as cursor:
            for table_name in FEATURE_TABLES:
                cursor.execute(
                    sql.SQL(
                        "SELECT MIN(feature_timestamp), MAX(feature_timestamp) "
                        "FROM {}.{}"
                    ).format(
                        sql.Identifier(config.schema),
                        sql.Identifier(table_name),
                    )
                )
                table_minimum, table_maximum = cursor.fetchone()
                if table_minimum is None or table_maximum is None:
                    raise RuntimeError(
                        f"Offline feature table is empty: {config.schema}.{table_name}"
                    )
                table_minimum = _as_utc(table_minimum)
                table_maximum = _as_utc(table_maximum)
                minimum = (
                    table_minimum if minimum is None else min(minimum, table_minimum)
                )
                maximum = (
                    table_maximum if maximum is None else max(maximum, table_maximum)
                )
    assert minimum is not None and maximum is not None
    return SourceFeatureBounds(
        start=minimum - timedelta(microseconds=1),
        end=maximum + timedelta(microseconds=1),
    )


def _online_feature_view_watermarks(store: Any) -> list[datetime | None]:
    return [
        _as_utc(view.most_recent_end_time)
        if view.most_recent_end_time is not None
        else None
        for view in store.list_feature_views()
        if getattr(view, "online", False)
    ]


def materialize_with_recovery(
    store: Any,
    bounds: SourceFeatureBounds,
    validate: Callable[[], dict[str, Any]],
) -> MaterializationResult:
    watermarks = _online_feature_view_watermarks(store)
    if not watermarks:
        raise RuntimeError("The Feast registry has no online feature views")

    mode = "noop"
    known_watermarks = [value for value in watermarks if value is not None]
    watermark_ahead = bool(known_watermarks) and any(
        value > bounds.end for value in known_watermarks
    )
    if watermark_ahead:
        store.materialize(
            bounds.start,
            bounds.end,
            disable_event_timestamp=True,
        )
        mode = "full_watermark_recovery"
    elif any(value is None or value < bounds.end for value in watermarks):
        store.materialize_incremental(bounds.end)
        mode = "incremental"

    report = validate()
    if report.get("status") != "SUCCESS":
        store.materialize(
            bounds.start,
            bounds.end,
            disable_event_timestamp=True,
        )
        mode = "full_online_store_recovery"
        report = validate()
    status = str(report.get("status", "ERROR"))
    if status != "SUCCESS":
        raise RuntimeError(
            "Feast online-store validation failed after recovery: "
            + json.dumps(report, sort_keys=True)
        )
    return MaterializationResult(
        mode=mode,
        source_start=bounds.start.isoformat(),
        source_end=bounds.end.isoformat(),
        validation_status=status,
    )


def run_materialization(repo_path: str = DEFAULT_REPO_PATH) -> MaterializationResult:
    from feast import FeatureStore
    from validate.governance_contracts import validate_feast_online_store

    configure_registry_url()
    store = FeatureStore(repo_path=repo_path)
    return materialize_with_recovery(
        store,
        source_feature_bounds(),
        validate_feast_online_store,
    )


def main() -> int:
    result = run_materialization(os.getenv("FEAST_REPO_PATH", DEFAULT_REPO_PATH))
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
