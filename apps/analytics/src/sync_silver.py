from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from validate.report_io import (
    check,
    dataset_result,
    validation_report,
    write_validation_report,
)


SILVER_SOURCE_TABLES = (
    "clean_behavior_events",
    "clean_impressions",
    "clean_recommendation_requests",
    "product_scd",
    "users",
    "products",
)

BRONZE_SOURCE_TABLES = ("orders", "order_items")
SOURCE_TABLES = SILVER_SOURCE_TABLES + BRONZE_SOURCE_TABLES


@dataclass(frozen=True)
class AnalyticsSyncConfig:
    source_catalog: str = "recsys"
    source_namespace: str = "lakehouse"
    source_warehouse: str = "s3a://recsys-lakehouse/warehouse"
    target_catalog: str = "analytics"
    target_namespace: str = "staging"
    target_warehouse: str = "s3a://recsys-lakehouse/analytics"
    jdbc_uri: str = (
        "jdbc:postgresql://recsys-analytics-catalog-postgres:5432/iceberg_catalog"
    )
    jdbc_user: str = "iceberg"
    jdbc_password: str = "iceberg"
    s3_endpoint: str = (
        "http://data-platform-minio.recsys-dataflow.svc.cluster.local:9000"
    )
    s3_access_key: str = "minio"
    s3_secret_key: str = "minio123"

    @classmethod
    def from_env(cls) -> "AnalyticsSyncConfig":
        return cls(
            source_catalog=os.getenv("ICEBERG_CATALOG", cls.source_catalog),
            source_namespace=os.getenv(
                "ICEBERG_LAKEHOUSE_NAMESPACE", cls.source_namespace
            ),
            source_warehouse=os.getenv("LAKEHOUSE_WAREHOUSE", cls.source_warehouse),
            target_catalog=os.getenv("ANALYTICS_ICEBERG_CATALOG", cls.target_catalog),
            target_namespace=os.getenv(
                "ANALYTICS_STAGING_SCHEMA", cls.target_namespace
            ),
            target_warehouse=os.getenv("ANALYTICS_WAREHOUSE", cls.target_warehouse),
            jdbc_uri=os.getenv("ANALYTICS_CATALOG_JDBC_URI", cls.jdbc_uri),
            jdbc_user=os.getenv("ANALYTICS_CATALOG_USER", cls.jdbc_user),
            jdbc_password=os.getenv("ANALYTICS_CATALOG_PASSWORD", cls.jdbc_password),
            s3_endpoint=os.getenv("MINIO_ENDPOINT", cls.s3_endpoint),
            s3_access_key=os.getenv(
                "AWS_ACCESS_KEY_ID", os.getenv("MINIO_ROOT_USER", cls.s3_access_key)
            ),
            s3_secret_key=os.getenv(
                "AWS_SECRET_ACCESS_KEY",
                os.getenv("MINIO_ROOT_PASSWORD", cls.s3_secret_key),
            ),
        )

    def source_table(self, name: str) -> str:
        layer = "bronze" if name in BRONZE_SOURCE_TABLES else "silver"
        return f"{self.source_catalog}.{self.source_namespace}.{layer}_{name}"

    def target_table(self, name: str) -> str:
        return f"{self.target_catalog}.{self.target_namespace}.{name}"


def spark_catalog_conf(config: AnalyticsSyncConfig) -> dict[str, str]:
    return {
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        f"spark.sql.catalog.{config.source_catalog}": "org.apache.iceberg.spark.SparkCatalog",
        f"spark.sql.catalog.{config.source_catalog}.type": "hadoop",
        f"spark.sql.catalog.{config.source_catalog}.warehouse": config.source_warehouse,
        f"spark.sql.catalog.{config.target_catalog}": "org.apache.iceberg.spark.SparkCatalog",
        f"spark.sql.catalog.{config.target_catalog}.type": "jdbc",
        f"spark.sql.catalog.{config.target_catalog}.uri": config.jdbc_uri,
        f"spark.sql.catalog.{config.target_catalog}.warehouse": config.target_warehouse,
        f"spark.sql.catalog.{config.target_catalog}.jdbc.user": config.jdbc_user,
        f"spark.sql.catalog.{config.target_catalog}.jdbc.password": config.jdbc_password,
        f"spark.sql.catalog.{config.target_catalog}.jdbc.schema-version": "V1",
        "spark.hadoop.fs.s3a.endpoint": config.s3_endpoint,
        "spark.hadoop.fs.s3a.access.key": config.s3_access_key,
        "spark.hadoop.fs.s3a.secret.key": config.s3_secret_key,
        "spark.hadoop.fs.s3a.path.style.access": "true",
        "spark.hadoop.fs.s3a.connection.ssl.enabled": "false",
    }


def build_spark(config: AnalyticsSyncConfig) -> Any:
    from pyspark.sql import SparkSession

    builder = SparkSession.builder.appName("recsys-analytics-silver-sync")
    for key, value in spark_catalog_conf(config).items():
        builder = builder.config(key, value)
    return builder.getOrCreate()


def redacted_config(config: AnalyticsSyncConfig) -> dict[str, str]:
    """Return log-safe sync settings without object-store or catalog credentials."""
    payload = asdict(config)
    for key in ("jdbc_password", "s3_access_key", "s3_secret_key"):
        payload[key] = "***"
    return payload


def sync_table(spark: Any, config: AnalyticsSyncConfig, table: str) -> tuple[int, int]:
    from pyspark.sql import functions as F

    frame = spark.table(config.source_table(table)).withColumn(
        "analytics_synced_at", F.current_timestamp()
    )
    frame.writeTo(config.target_table(table)).using("iceberg").createOrReplace()
    source_count = frame.count()
    target_count = spark.table(config.target_table(table)).count()
    return source_count, target_count


def run(
    config: AnalyticsSyncConfig | None = None,
    *,
    report_uri: str = "",
    run_id: str = "manual",
) -> dict[str, Any]:
    config = config or AnalyticsSyncConfig.from_env()
    spark = build_spark(config)
    counts: dict[str, int] = {}
    datasets: dict[str, dict[str, Any]] = {}
    try:
        spark.sql(
            f"CREATE NAMESPACE IF NOT EXISTS {config.target_catalog}.{config.target_namespace}"
        )
        for table in SOURCE_TABLES:
            try:
                source_count, target_count = sync_table(spark, config, table)
                counts[table] = target_count
                checks = [
                    check(
                        "row_count",
                        "SUCCESS" if target_count > 0 else "FAILURE",
                        "> 0",
                        target_count,
                    ),
                    check(
                        "source_target_count",
                        "SUCCESS" if source_count == target_count else "FAILURE",
                        source_count,
                        target_count,
                    ),
                ]
            except Exception as exc:
                checks = [check("table_sync", "ERROR", "completed", str(exc))]
            datasets[f"analytics.staging.{table}"] = dataset_result(checks)
        if report_uri:
            write_validation_report(
                validation_report("ANALYTICS", run_id, datasets, report_uri=report_uri),
                report_uri,
            )
        if any(value["status"] != "SUCCESS" for value in datasets.values()):
            raise RuntimeError("Analytics staging validation failed")
        return {"status": "ok", "tables": counts, "config": redacted_config(config)}
    finally:
        spark.stop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync and validate analytics staging datasets"
    )
    parser.add_argument("--report-uri", default="")
    parser.add_argument("--run-id", default="manual")
    args = parser.parse_args()
    try:
        result = run(report_uri=args.report_uri, run_id=args.run_id)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
