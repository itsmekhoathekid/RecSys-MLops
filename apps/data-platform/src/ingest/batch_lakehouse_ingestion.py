from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pyarrow.fs as pafs

from features.spark.session import read_parquet_table, row_count, spark_session, write_iceberg_table
from lakehouse.iceberg import IcebergCatalogConfig, RAW_GENERATOR_TABLES, create_spark_namespace
from metadata.governance_catalog import BRONZE_URNS
from metadata.runtime_lineage import RuntimeLineageRecorder


@dataclass(frozen=True)
class LakehouseIcebergLayout:
    catalog_name: str = os.getenv("ICEBERG_CATALOG", "recsys")
    namespace: str = os.getenv("ICEBERG_LAKEHOUSE_NAMESPACE", "lakehouse")

    def table_name(self, source_table: str) -> str:
        return f"{self.catalog_name}.{self.namespace}.bronze_{source_table}"


def infer_run_id(run_path: str | Path, explicit_run_id: str | None = None) -> str:
    if explicit_run_id:
        return explicit_run_id
    return Path(str(run_path).rstrip("/")).name


def _normalise_uri(uri: str | Path) -> str:
    value = str(uri)
    if value.startswith("s3a://"):
        return "s3://" + value.removeprefix("s3a://")
    return value


def _s3_endpoint() -> tuple[str, str]:
    endpoint = os.getenv("MINIO_ENDPOINT", os.getenv("DATA_PLATFORM_MINIO_ENDPOINT", "http://data-platform-minio:9000"))
    if "://" not in endpoint:
        return "http", endpoint
    parsed = urlparse(endpoint)
    return parsed.scheme, parsed.netloc


def _filesystem_and_path(uri: str | Path) -> tuple[pafs.FileSystem, str]:
    normalised = _normalise_uri(uri)
    parsed = urlparse(normalised)
    if parsed.scheme == "s3":
        scheme, endpoint = _s3_endpoint()
        return (
            pafs.S3FileSystem(
                access_key=os.getenv("AWS_ACCESS_KEY_ID", os.getenv("MINIO_ROOT_USER", "minio")),
                secret_key=os.getenv("AWS_SECRET_ACCESS_KEY", os.getenv("MINIO_ROOT_PASSWORD", "minio123")),
                region=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                scheme=scheme,
                endpoint_override=endpoint,
            ),
            f"{parsed.netloc}{parsed.path}",
        )
    if parsed.scheme:
        raise ValueError(f"Unsupported lakehouse URI scheme: {parsed.scheme}")
    return pafs.LocalFileSystem(), str(Path(normalised))


def load_generator_run_to_lakehouse(
    run_path: str | Path,
    *,
    spark=None,
    catalog: IcebergCatalogConfig | None = None,
    layout: LakehouseIcebergLayout | None = None,
    mode: str = "overwrite",
    run_id: str | None = None,
) -> dict[str, int]:
    from pyspark.sql import functions as F

    catalog = catalog or IcebergCatalogConfig()
    layout = layout or LakehouseIcebergLayout(catalog.catalog_name, catalog.lakehouse_namespace)
    owns_spark = spark is None
    spark = spark or spark_session("recsys-dp1-generator-to-iceberg")
    source_run_id = infer_run_id(run_path, run_id)
    ingestion_ts = datetime.now(timezone.utc)
    try:
        create_spark_namespace(spark, catalog)
        with RuntimeLineageRecorder("DP1", "ingest_stage") as lineage:
            counts: dict[str, int] = {}
            for table_name in RAW_GENERATOR_TABLES:
                frame = read_parquet_table(spark, str(run_path), table_name)
                frame = frame.withColumn("source_run_id", F.lit(source_run_id)).withColumn(
                    "lakehouse_ingestion_ts",
                    F.lit(ingestion_ts).cast("timestamp"),
                )
                counts[table_name] = row_count(frame)
                write_iceberg_table(frame, layout.table_name(table_name), mode=mode)
                lineage.add_outputs(BRONZE_URNS[table_name])
            return counts
    finally:
        if owns_spark:
            spark.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest generated batch data into governed Bronze Iceberg tables")
    parser.add_argument("--run-path", default=os.getenv("GENERATOR_RUN_PATH", "apps/data-platform/data-generator/src/output/test_10k_seed42"))
    parser.add_argument("--run-id", default=os.getenv("GENERATOR_RUN_ID"))
    parser.add_argument("--mode", choices=["append", "overwrite"], default=os.getenv("LAKEHOUSE_INGEST_MODE", "overwrite"))
    parser.add_argument("--lakehouse-warehouse", default=os.getenv("LAKEHOUSE_WAREHOUSE", "s3a://recsys-lakehouse/warehouse"))
    parser.add_argument("--iceberg-lakehouse-namespace", default=os.getenv("ICEBERG_LAKEHOUSE_NAMESPACE", "lakehouse"))
    args = parser.parse_args()
    catalog = IcebergCatalogConfig(
        warehouse_uri=args.lakehouse_warehouse,
        lakehouse_namespace=args.iceberg_lakehouse_namespace,
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
