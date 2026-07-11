from __future__ import annotations

import argparse
import json
from typing import Any

from features.spark.session import compact_iceberg_table, spark_session
from lakehouse.iceberg import FEATURE_TABLES, SILVER_LAKEHOUSE_TABLES, IcebergCatalogConfig


# Z-order is reserved for tables whose dominant access path uses these columns.
# Tables without a profile are still compacted with Iceberg bin-packing.
ZORDER_COLUMNS: dict[str, tuple[str, ...]] = {
    "silver_clean_behavior_events": ("user_id", "product_id", "event_timestamp"),
    "silver_clean_impressions": ("user_id", "candidate_product_id", "impression_timestamp"),
    "user_sequence_features": ("user_id", "feature_timestamp"),
    "user_aggregate_features": ("user_id", "feature_timestamp"),
    "item_features": ("product_id", "feature_timestamp"),
    "ml_ranking_labels": ("user_id", "candidate_product_id", "prediction_timestamp"),
    "ml_bst_training": ("user_id", "target_item_id", "prediction_timestamp"),
}


def optimization_tables(scope: str, catalog: IcebergCatalogConfig) -> list[str]:
    tables: list[str] = []
    if scope in {"silver", "all"}:
        tables.extend(catalog.lakehouse_table(f"silver_{name}") for name in SILVER_LAKEHOUSE_TABLES)
    if scope in {"features", "all"}:
        tables.extend(catalog.feature_table(name) for name in FEATURE_TABLES.values())
    return tables


def _sort_columns(table_name: str, strategy: str) -> tuple[str, ...]:
    if strategy == "binpack":
        return ()
    return ZORDER_COLUMNS.get(table_name.rsplit(".", 1)[-1], ())


def _is_missing_table_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("table_or_view_not_found", "no such table", "cannot find table", "does not exist")
    )


def optimize_lakehouse(
    spark: Any,
    *,
    scope: str = "all",
    strategy: str = "binpack",
    target_file_size_bytes: int = 134_217_728,
    min_input_files: int = 2,
    rewrite_all: bool = False,
    skip_missing: bool = False,
    catalog: IcebergCatalogConfig | None = None,
) -> dict[str, Any]:
    catalog = catalog or IcebergCatalogConfig()
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for table_name in optimization_tables(scope, catalog):
        try:
            results.append(
                compact_iceberg_table(
                    spark,
                    table_name,
                    target_file_size_bytes,
                    min_input_files=min_input_files,
                    sort_columns=_sort_columns(table_name, strategy),
                    rewrite_all=rewrite_all,
                )
            )
        except Exception as exc:
            if not skip_missing or not _is_missing_table_error(exc):
                raise
            skipped.append({"table": table_name, "reason": str(exc)})

    before_files = sum(int(result["before"]["file_count"]) for result in results)
    after_files = sum(int(result["after"]["file_count"]) for result in results)
    return {
        "status": "SUCCESS",
        "scope": scope,
        "requested_strategy": strategy,
        "tables_optimized": len(results),
        "tables_skipped": skipped,
        "before_file_count": before_files,
        "after_file_count": after_files,
        "file_count_reduction": before_files - after_files,
        "tables": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact and cluster the RecSys Iceberg lakehouse")
    parser.add_argument("--scope", choices=("silver", "features", "all"), default="all")
    parser.add_argument("--strategy", choices=("binpack", "zorder"), default="binpack")
    parser.add_argument("--target-file-size-mb", type=int, default=128)
    parser.add_argument("--min-input-files", type=int, default=2)
    parser.add_argument("--rewrite-all", action="store_true")
    parser.add_argument("--skip-missing", action="store_true")
    args = parser.parse_args()

    spark = spark_session("recsys-lakehouse-optimization")
    try:
        report = optimize_lakehouse(
            spark,
            scope=args.scope,
            strategy=args.strategy,
            target_file_size_bytes=args.target_file_size_mb * 1024 * 1024,
            min_input_files=args.min_input_files,
            rewrite_all=args.rewrite_all,
            skip_missing=args.skip_missing,
        )
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    finally:
        spark.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
