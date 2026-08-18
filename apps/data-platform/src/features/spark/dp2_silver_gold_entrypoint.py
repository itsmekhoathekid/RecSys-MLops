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
from validate.report_io import validation_report, write_validation_report


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
def validate_dp2_silver_gold() -> dict[str, Any]:
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
            count_ok = (
                counts[table_name] >= 0
                if table_name == "rejected_behavior_events"
                else counts[table_name] > 0
            )
            checks = [
                check(
                    "row_count",
                    "SUCCESS" if count_ok else "FAILURE",
                    expected,
                    counts[table_name],
                )
            ]
            if table_name == "clean_behavior_events":
                duplicate_count = (
                    counts[table_name] - frame.select("event_id").distinct().count()
                )
                checks.extend(
                    [
                        check(
                            "required_columns",
                            "SUCCESS"
                            if {"event_id", "event_timestamp", "ingestion_ts"}.issubset(
                                frame.columns
                            )
                            else "FAILURE",
                            ["event_id", "event_timestamp", "ingestion_ts"],
                            sorted(frame.columns),
                        ),
                        check(
                            "duplicate_event_id",
                            "SUCCESS" if duplicate_count == 0 else "FAILURE",
                            0,
                            duplicate_count,
                        ),
                    ]
                )
            datasets[f"silver.{table_name}"] = dataset_result(checks)
        report = build_validation_report("DP2", datasets)
        report["counts"] = counts
        return report
    finally:
        spark.stop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run or validate the DP2 bronze-to-silver/gold Spark pipeline."
    )
    parser.add_argument("--action", choices=("ingest", "validate"), required=True)
    parser.add_argument("--report-uri", default="")
    parser.add_argument("--run-id", default="manual")
    args = parser.parse_args()

    result: dict[str, Any]
    if args.action == "ingest":
        result = {"dp2_ingest_silver_gold_counts": build_dp2_silver_gold()}
    else:
        try:
            report = validate_dp2_silver_gold()
        except Exception as exc:
            report = build_validation_report(
                "DP2",
                {
                    f"silver.{table}": dataset_result(
                        [check("validation_execution", "ERROR", "completed", str(exc))]
                    )
                    for table in SILVER_LAKEHOUSE_TABLES
                },
            )
        if args.report_uri:
            write_validation_report(
                validation_report(
                    "DP2", args.run_id, report["datasets"], report_uri=args.report_uri
                ),
                args.report_uri,
            )
        result = {"dp2_validate_silver_gold": report}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if args.action == "ingest" or report["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
