from __future__ import annotations

import argparse
import json
from typing import Any

from features.spark.build_silver_tables import build_silver_tables
from features.spark.session import read_iceberg_table, row_count, spark_session
from lakehouse.iceberg import (
    IcebergCatalogConfig,
    SILVER_LAKEHOUSE_TABLES,
    create_spark_namespace,
)
from validate.governance_contracts import build_validation_report, check, dataset_result


def build_dp2_silver_gold() -> dict[str, int]:
    spark = spark_session("recsys-dp2-bronze-to-silver-gold")
    catalog = IcebergCatalogConfig()
    try:
        create_spark_namespace(spark, catalog)
        silver = build_silver_tables(spark, catalog=catalog, source="lakehouse")
        return {name: row_count(frame) for name, frame in sorted(silver.items())}
    finally:
        spark.stop()

# DP2 validates Silver row counts plus required clean-event columns and unique event_id values.
def validate_dp2_silver_gold() -> dict[str, int]:
    spark = spark_session("recsys-dp2-validate-silver-gold")
    catalog = IcebergCatalogConfig()
    try:
        counts: dict[str, int] = {}
        datasets: dict[str, dict[str, Any]] = {}
        for table_name in SILVER_LAKEHOUSE_TABLES:
            full_name = catalog.lakehouse_table(f"silver_{table_name}")
            frame = read_iceberg_table(spark, full_name)
            counts[table_name] = row_count(frame)
            expected = ">= 0" if table_name == "rejected_behavior_events" else "> 0"
            count_ok = counts[table_name] >= 0 if table_name == "rejected_behavior_events" else counts[table_name] > 0
            checks = [check("row_count", "SUCCESS" if count_ok else "FAILURE", expected, counts[table_name])]
            if table_name == "clean_behavior_events":
                duplicate_count = counts[table_name] - frame.select("event_id").distinct().count()
                checks.extend([
                    check(
                        "required_columns",
                        "SUCCESS" if {"event_id", "event_timestamp", "ingestion_ts"}.issubset(frame.columns) else "FAILURE",
                        ["event_id", "event_timestamp", "ingestion_ts"],
                        sorted(frame.columns),
                    ),
                    check("duplicate_event_id", "SUCCESS" if duplicate_count == 0 else "FAILURE", 0, duplicate_count),
                ])
            datasets[f"silver.{table_name}"] = dataset_result(checks)
        report = build_validation_report("DP2", datasets)
        if report["status"] != "SUCCESS":
            raise AssertionError(f"DP2 validation failed: {report}")
        return counts
    finally:
        spark.stop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run or validate the DP2 bronze-to-silver/gold Spark pipeline."
    )
    parser.add_argument("--action", choices=("ingest", "validate"), required=True)
    args = parser.parse_args()

    result: dict[str, Any]
    if args.action == "ingest":
        result = {"dp2_ingest_silver_gold_counts": build_dp2_silver_gold()}
    else:
        result = {"dp2_validate_silver_gold_counts": validate_dp2_silver_gold()}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
