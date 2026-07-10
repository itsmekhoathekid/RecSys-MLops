from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from config import GeneratorConfig, load_config
from sink import read_table


ID_COLUMNS = {
    "users": "user_id",
    "products": "product_id",
    "sessions": "session_id",
    "recommendation_requests": "request_id",
    "impressions": "impression_id",
    "behavior_events": "event_id",
    "orders": "order_id",
    "order_items": "order_item_id",
}


def percent(numerator: int | float, denominator: int | float) -> str:
    if not denominator:
        return "0.00%"
    return f"{(numerator / denominator) * 100:.2f}%"


def table_rows(run_path: Path, table_name: str) -> list[dict[str, Any]]:
    return read_table(run_path, table_name).to_pylist()


def top_counts(
    rows: list[dict[str, Any]], column: str, limit: int = 5
) -> list[tuple[Any, int, str]]:
    counter = Counter(row[column] for row in rows)
    total = len(rows)
    return [(value, count, percent(count, total)) for value, count in counter.most_common(limit)]


def in_burst_window(hour: int, windows: list[Any]) -> bool:
    for window in windows:
        if window.start_hour <= window.end_hour:
            if window.start_hour <= hour < window.end_hour:
                return True
        elif hour >= window.start_hour or hour < window.end_hour:
            return True
    return False


def parquet_storage_summary(path: Path) -> dict[str, Any]:
    files = sorted(path.rglob("*.parquet")) if path.exists() else []
    compression = Counter()
    for file_path in files:
        metadata = pq.ParquetFile(file_path).metadata
        for row_group_index in range(metadata.num_row_groups):
            row_group = metadata.row_group(row_group_index)
            for column_index in range(row_group.num_columns):
                compression[row_group.column(column_index).compression] += 1
    return {
        "exists": path.exists(),
        "file_count": len(files),
        "total_bytes": sum(file_path.stat().st_size for file_path in files),
        "compression": ", ".join(
            f"{name}:{count}" for name, count in sorted(compression.items())
        )
        or "n/a",
    }


def partitioned_tables(run_path: Path) -> list[str]:
    tables: list[str] = []
    for table_dir in sorted(path for path in run_path.iterdir() if path.is_dir()):
        if any(child.name.startswith("business_date=") for child in table_dir.iterdir()):
            tables.append(table_dir.name)
    return tables


def print_section(title: str) -> None:
    print()
    print(f"## {title}")


def print_markdown_table(headers: list[str], rows: list[list[Any]]) -> None:
    table = [[str(value) for value in headers]]
    table.extend([[str(value) for value in row] for row in rows])
    column_count = max(len(row) for row in table)
    table = [row + [""] * (column_count - len(row)) for row in table]
    widths = [
        max(3, max(len(row[column_index]) for row in table))
        for column_index in range(column_count)
    ]

    def format_row(row: list[str]) -> str:
        return (
            "| "
            + " | ".join(
                row[column_index].ljust(widths[column_index])
                for column_index in range(column_count)
            )
            + " |"
        )

    print(format_row(table[0]))
    print("| " + " | ".join("-" * width for width in widths) + " |")
    for row in table[1:]:
        print(format_row(row))


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize(config: GeneratorConfig, config_path: Path, lake_root: Path | None) -> None:
    run_path = Path(config.output.base_path) / config.output.run_id
    manifest = load_json(run_path / "manifest.json")
    dq_report = load_json(run_path / "data_quality_report.json")

    users = table_rows(run_path, "users")
    products = table_rows(run_path, "products")
    events = table_rows(run_path, "behavior_events")

    row_counts = manifest["row_counts"]
    raw_event_count = len(events)
    event_ids = [row["event_id"] for row in events]
    distinct_event_ids = len(set(event_ids))
    duplicate_rows = raw_event_count - distinct_event_ids
    exact_duplicate_rows = sum(
        count - 1
        for count in Counter(
            (row["event_id"], row["payload_hash"]) for row in events
        ).values()
        if count > 1
    )
    payload_hashes_by_event_id: dict[Any, set[str]] = defaultdict(set)
    for row in events:
        payload_hashes_by_event_id[row["event_id"]].add(row["payload_hash"])
    conflicting_duplicate_ids = sum(
        len(payload_hashes) > 1
        for payload_hashes in payload_hashes_by_event_id.values()
    )

    change_date = config.schema_evolution.change_date
    breaking_change_date = config.schema_evolution.breaking_change_date
    old_partition_rows = [
        row for row in events if row["event_timestamp"].date() < change_date
    ]
    new_partition_rows = [
        row
        for row in events
        if row["event_timestamp"].date() >= change_date
        and (
            breaking_change_date is None
            or row["event_timestamp"].date() < breaking_change_date
        )
    ]
    breaking_partition_rows = [
        row
        for row in events
        if breaking_change_date is not None
        and row["event_timestamp"].date() >= breaking_change_date
    ]

    late_threshold_seconds = config.challenges.late_delay_minutes_min * 60
    late_rows = sum(
        (row["created_ts"] - row["event_timestamp"]).total_seconds()
        >= late_threshold_seconds
        for row in events
    )
    out_of_order_rows = sum(
        (row["ingestion_ts"] - max(row["created_ts"], row["event_timestamp"]))
        .total_seconds()
        > 50
        for row in events
    )
    burst_rows = sum(
        in_burst_window(row["event_timestamp"].hour, config.burst_windows)
        for row in events
    )

    run_storage = parquet_storage_summary(run_path)
    lake_path = (
        lake_root / "raw" / config.output.run_id
        if lake_root is not None
        else None
    )
    lake_storage = parquet_storage_summary(lake_path) if lake_path else None

    print("# Data Generator Evidence")
    print(f"- Generated at: {datetime.now().isoformat(timespec='seconds')}")
    print(f"- Config: `{config_path}`")
    print(f"- Run ID: `{config.output.run_id}`")
    print(f"- Output path: `{run_path}`")
    print(f"- Validation passed: `{dq_report['validation_passed']}`")
    print(f"- Validation errors: `{len(dq_report['validation_errors'])}`")

    print_section("Generator Config")
    print_markdown_table(
        ["setting", "value"],
        [
            ["seed", config.seed],
            ["history_start_date", config.history_start_date],
            ["history_days", config.history_days],
            ["n_users", config.entities.n_users],
            ["n_products", config.entities.n_products],
            ["n_categories", config.entities.n_categories],
            ["n_brands", config.entities.n_brands],
            ["target_behavior_events", config.traffic.target_behavior_events],
            ["top_city_ratio", config.distribution.top_city_ratio],
            ["top_category_ratio", config.distribution.top_category_ratio],
            ["duplicate_event_rate", config.challenges.duplicate_event_rate],
            ["conflicting_duplicate_rate", config.challenges.conflicting_duplicate_rate],
            ["late_arrival_rate", config.challenges.late_arrival_rate],
            ["out_of_order_rate", config.challenges.out_of_order_rate],
            [
                "burst_windows",
                ", ".join(
                    f"{window.start_hour}:00-{window.end_hour}:00 x{window.traffic_weight}"
                    for window in config.burst_windows
                )
                or "none",
            ],
            ["schema_change_date", config.schema_evolution.change_date],
            ["breaking_schema_change_date", config.schema_evolution.breaking_change_date],
            ["breaking_schema_version", config.schema_evolution.breaking_schema_version],
        ],
    )

    print_section("Data Volume And Storage")
    print_markdown_table(
        ["table", "rows"],
        [[table_name, count] for table_name, count in sorted(row_counts.items())],
    )
    print()
    print_markdown_table(
        ["location", "format", "parquet_files", "bytes", "compression", "partitioning"],
        [
            [
                str(run_path),
                "Parquet",
                run_storage["file_count"],
                run_storage["total_bytes"],
                run_storage["compression"],
                "business_date for "
                + ", ".join(partitioned_tables(run_path)),
            ],
            *(
                [
                    [
                        str(lake_path),
                        "MinIO-like raw layout",
                        lake_storage["file_count"],
                        lake_storage["total_bytes"],
                        lake_storage["compression"],
                        "raw/<run_id>/<table>/...",
                    ]
                ]
                if lake_path is not None and lake_storage is not None
                else []
            ),
        ],
    )

    print_section("Skew Distribution")
    skew_rows = [["city", value, count, pct] for value, count, pct in top_counts(users, "city")]
    skew_rows.extend(
        ["event_category_id", value, count, pct]
        for value, count, pct in top_counts(events, "category_id")
    )
    skew_rows.extend(
        ["product_category_id", value, count, pct]
        for value, count, pct in top_counts(products, "category_id")
    )
    print_markdown_table(["dimension", "value", "count", "pct"], skew_rows)

    print_section("Cardinality")
    cardinality_rows: list[list[Any]] = []
    for table_name, id_column in ID_COLUMNS.items():
        rows = table_rows(run_path, table_name)
        approx_distinct = len({row[id_column] for row in rows if row[id_column] is not None})
        cardinality_rows.append(
            [
                table_name,
                id_column,
                len(rows),
                approx_distinct,
                percent(approx_distinct, len(rows)),
            ]
        )
    print_markdown_table(
        ["table", "id_column", "rows", "approx_count_distinct", "distinct_ratio"],
        cardinality_rows,
    )

    print_section("Schema Evolution")
    print_markdown_table(
        [
            "partition_group",
            "rows",
            "schema_versions",
            "device_type_nulls",
            "campaign_id_nulls",
            "null_pct",
        ],
        [
            [
                f"old partitions before {change_date}",
                len(old_partition_rows),
                sorted({row["schema_version"] for row in old_partition_rows}),
                sum(row["device_type"] is None for row in old_partition_rows),
                sum(row["campaign_id"] is None for row in old_partition_rows),
                percent(
                    sum(row["device_type"] is None for row in old_partition_rows),
                    len(old_partition_rows),
                ),
            ],
            [
                f"new partitions from {change_date}",
                len(new_partition_rows),
                sorted({row["schema_version"] for row in new_partition_rows}),
                sum(row["device_type"] is None for row in new_partition_rows),
                sum(row["campaign_id"] is None for row in new_partition_rows),
                percent(
                    sum(row["device_type"] is None for row in new_partition_rows),
                    len(new_partition_rows),
                ),
            ],
            *(
                [
                    [
                        f"breaking partitions from {breaking_change_date}",
                        len(breaking_partition_rows),
                        sorted(
                            {row["schema_version"] for row in breaking_partition_rows}
                        ),
                        sum(row["device_type"] is None for row in breaking_partition_rows),
                        sum(row["campaign_id"] is None for row in breaking_partition_rows),
                        percent(
                            sum(
                                row["device_type"] is None
                                for row in breaking_partition_rows
                            ),
                            len(breaking_partition_rows),
                        ),
                    ]
                ]
                if breaking_change_date is not None
                else []
            ),
        ],
    )

    print_section("Duplicate Rate Before And After Dedup")
    print_markdown_table(
        ["stage", "rows", "distinct_event_ids", "duplicate_rows", "duplicate_rate"],
        [
            [
                "before_dedup",
                raw_event_count,
                distinct_event_ids,
                duplicate_rows,
                percent(duplicate_rows, raw_event_count),
            ],
            [
                "after_dedup_by_event_id",
                distinct_event_ids,
                distinct_event_ids,
                0,
                "0.00%",
            ],
        ],
    )
    print()
    print_markdown_table(
        ["duplicate_type", "observed_count", "observed_rate"],
        [
            [
                "exact duplicate rows",
                exact_duplicate_rows,
                percent(exact_duplicate_rows, raw_event_count),
            ],
            [
                "conflicting duplicate event ids",
                conflicting_duplicate_ids,
                percent(conflicting_duplicate_ids, distinct_event_ids),
            ],
        ],
    )

    print_section("Streaming Problems")
    print_markdown_table(
        ["problem", "count", "rate", "proof"],
        [
            [
                "burst events in configured windows",
                burst_rows,
                percent(burst_rows, raw_event_count),
                ", ".join(
                    f"{window.start_hour}:00-{window.end_hour}:00 x{window.traffic_weight}"
                    for window in config.burst_windows
                )
                or "none",
            ],
            [
                "late arrivals",
                late_rows,
                percent(late_rows, raw_event_count),
                f"created_ts-event_timestamp >= {config.challenges.late_delay_minutes_min} minutes",
            ],
            [
                "stream duplicate rows",
                duplicate_rows,
                percent(duplicate_rows, raw_event_count),
                "same event_id appears more than once",
            ],
            [
                "out-of-order ingestion",
                out_of_order_rows,
                percent(out_of_order_rows, raw_event_count),
                "ingestion_ts is delayed > 50 seconds after created_ts/event_ts",
            ],
        ],
    )

    print_section("Injected Vs Observed Quality Metrics")
    print("```json")
    print(json.dumps(dq_report, indent=2, sort_keys=True))
    print("```")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print Markdown evidence for generated data quality."
    )
    parser.add_argument("--config", required=True, help="Path to generator YAML config")
    parser.add_argument(
        "--lake-root",
        default=None,
        help="Optional local MinIO-like lake root used by generate_historical_to_minio.py",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    lake_root = Path(args.lake_root) if args.lake_root else None
    summarize(config, config_path, lake_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
