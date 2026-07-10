from __future__ import annotations

import argparse
import json
from typing import Any

from features.spark.build_silver_tables import build_silver_tables
from features.spark.session import read_iceberg_table, row_count, spark_session
from lakehouse.iceberg import IcebergCatalogConfig, SILVER_LAKEHOUSE_TABLES, create_spark_namespace


def build_dp2_silver_gold() -> dict[str, int]:
    spark = spark_session("recsys-dp2-bronze-to-silver-gold")
    catalog = IcebergCatalogConfig()
    try:
        create_spark_namespace(spark, catalog)
        silver = build_silver_tables(spark, catalog=catalog, source="lakehouse")
        return {name: row_count(frame) for name, frame in sorted(silver.items())}
    finally:
        spark.stop()


def validate_dp2_silver_gold() -> dict[str, int]:
    spark = spark_session("recsys-dp2-validate-silver-gold")
    catalog = IcebergCatalogConfig()
    try:
        counts: dict[str, int] = {}
        for table_name in SILVER_LAKEHOUSE_TABLES:
            full_name = catalog.lakehouse_table(f"silver_{table_name}")
            counts[table_name] = row_count(read_iceberg_table(spark, full_name))
        missing = {name: count for name, count in counts.items() if count <= 0}
        if missing:
            raise AssertionError(f"DP2 silver/gold tables are empty: {missing}; counts={counts}")
        return counts
    finally:
        spark.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run or validate the DP2 bronze-to-silver/gold Spark pipeline.")
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
