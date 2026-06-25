from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from feature_engineering.spark.session import read_parquet_table, row_count, spark_session, write_iceberg_table
from lakehouse.iceberg import IcebergCatalogConfig, RAW_GENERATOR_TABLES, create_spark_namespace


def infer_run_id(run_path: str | Path, explicit_run_id: str | None = None) -> str:
    if explicit_run_id:
        return explicit_run_id
    return Path(str(run_path).rstrip("/")).name


def load_generator_run_to_lakehouse(
    run_path: str | Path,
    *,
    catalog: IcebergCatalogConfig | None = None,
    mode: str = "overwrite",
    run_id: str | None = None,
) -> dict[str, int]:
    from pyspark.sql import functions as F

    spark = spark_session("recsys-generator-batch-ingestion-to-lakehouse")
    catalog = catalog or IcebergCatalogConfig()
    create_spark_namespace(spark, catalog)
    source_run_id = infer_run_id(run_path, run_id)
    counts: dict[str, int] = {}
    try:
        for table_name in RAW_GENERATOR_TABLES:
            frame = read_parquet_table(spark, str(run_path), table_name)
            enriched = (
                frame.withColumn("source_run_id", F.lit(source_run_id))
                .withColumn("lakehouse_ingestion_ts", F.current_timestamp())
            )
            write_iceberg_table(enriched, catalog.lakehouse_table(table_name), mode=mode)
            counts[table_name] = row_count(enriched)
    finally:
        spark.stop()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest generated batch parquet data into Iceberg lakehouse tables")
    parser.add_argument("--run-path", default=os.getenv("GENERATOR_RUN_PATH", "apps/data-platform/data-generator/src/output/test_10k_seed42"))
    parser.add_argument("--run-id", default=os.getenv("GENERATOR_RUN_ID"))
    parser.add_argument("--mode", choices=["append", "overwrite"], default=os.getenv("LAKEHOUSE_INGEST_MODE", "overwrite"))
    parser.add_argument("--lakehouse-warehouse", default=os.getenv("LAKEHOUSE_WAREHOUSE", "s3a://recsys-lakehouse/warehouse"))
    parser.add_argument("--iceberg-catalog", default=os.getenv("ICEBERG_CATALOG", "recsys"))
    parser.add_argument("--iceberg-lakehouse-namespace", default=os.getenv("ICEBERG_LAKEHOUSE_NAMESPACE", "lakehouse"))
    parser.add_argument("--iceberg-feature-namespace", default=os.getenv("ICEBERG_FEATURE_NAMESPACE", "feature_store"))
    args = parser.parse_args()
    catalog = IcebergCatalogConfig(
        catalog_name=args.iceberg_catalog,
        lakehouse_namespace=args.iceberg_lakehouse_namespace,
        feature_namespace=args.iceberg_feature_namespace,
        warehouse_uri=args.lakehouse_warehouse,
    )
    print(
        json.dumps(
            load_generator_run_to_lakehouse(
                args.run_path,
                catalog=catalog,
                mode=args.mode,
                run_id=args.run_id,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
