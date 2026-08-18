from __future__ import annotations

import argparse
import json
import os
from typing import Any

from psycopg import sql

from feature_store.postgres_offline_store import (
    TABLE_SCHEMAS,
    PostgresOfflineStoreConfig,
)
from ingest.postgres_cdc_contracts import SOURCE_TABLE_CONTRACTS
from features.spark.session import read_iceberg_table, row_count, spark_session
from lakehouse.iceberg import IcebergCatalogConfig, RAW_GENERATOR_TABLES
from validate.report_io import (
    check,
    dataset_result,
    validation_report,
    write_validation_report,
)


EXPECTED_DATASET_KEYS = {
    "dp1": tuple(f"bronze.{table}" for table in RAW_GENERATOR_TABLES),
    "dp3-postgres": tuple(
        f"postgres.feature_store.{table}"
        for table in (
            "user_sequence_features",
            "user_aggregate_features",
            "item_features",
            "ml_ranking_labels",
        )
    ),
    "streaming-redis": tuple(
        f"redis.{table}"
        for table in (
            "user_sequence_features",
            "user_aggregate_features",
            "item_features",
        )
    ),
    "feast-online": tuple(
        f"redis.{table}"
        for table in (
            "user_sequence_features",
            "user_aggregate_features",
            "item_features",
        )
    ),
}


def build_validation_report(
    pipeline: str, datasets: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    statuses = {result["status"] for result in datasets.values()}
    status = (
        "ERROR"
        if "ERROR" in statuses
        else "FAILURE"
        if "FAILURE" in statuses
        else "SUCCESS"
    )
    return {"pipeline": pipeline, "status": status, "datasets": datasets}


# DP1 validates readable, non-empty Bronze tables with source/audit keys present and non-null.
def validate_dp1_bronze(*, spark=None) -> dict[str, Any]:
    from functools import reduce
    from operator import or_

    from pyspark.sql import functions as F

    catalog = IcebergCatalogConfig()
    owns_spark = spark is None
    spark = spark or spark_session("recsys-dp1-validate-bronze-iceberg")
    primary_keys = {
        contract.table_name: contract.primary_key for contract in SOURCE_TABLE_CONTRACTS
    }
    datasets: dict[str, dict[str, Any]] = {}
    for table_name in RAW_GENERATOR_TABLES:
        try:
            table = read_iceberg_table(spark, catalog.bronze_table(table_name))
            required = set(primary_keys[table_name]) | {
                "source_run_id",
                "lakehouse_ingestion_ts",
            }
            missing = sorted(required.difference(table.columns))
            count = row_count(table)
            null_count = (
                table.filter(
                    reduce(or_, (F.col(name).isNull() for name in required))
                ).count()
                if not missing
                else -1
            )
            checks = [
                check("row_count", "SUCCESS" if count > 0 else "FAILURE", "> 0", count),
                check(
                    "required_columns",
                    "SUCCESS" if not missing else "FAILURE",
                    sorted(required),
                    {"missing": missing},
                ),
                check(
                    "required_values_not_null",
                    "SUCCESS" if null_count == 0 else "FAILURE",
                    0,
                    null_count,
                ),
            ]
        except Exception as exc:
            checks = [
                check("table_read", "ERROR", "readable Bronze Iceberg table", str(exc))
            ]
        datasets[f"bronze.{table_name}"] = dataset_result(checks)
    report = build_validation_report("DP1", datasets)
    if owns_spark:
        spark.stop()
    return report


# DP3 validates complete, non-empty PostgreSQL tables with non-null entity keys and timestamps.
def validate_dp3_postgres() -> dict[str, Any]:
    config = PostgresOfflineStoreConfig.from_env()
    primary_keys = {
        "user_sequence_features": "user_id",
        "user_aggregate_features": "user_id",
        "item_features": "product_id",
        "ml_ranking_labels": "impression_id",
    }
    timestamp_columns = {
        "user_sequence_features": "feature_timestamp",
        "user_aggregate_features": "feature_timestamp",
        "item_features": "feature_timestamp",
        "ml_ranking_labels": "prediction_timestamp",
    }
    datasets: dict[str, dict[str, Any]] = {}
    with config.connect() as conn:
        with conn.cursor() as cur:
            for table_name in primary_keys:
                try:
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
                        (config.schema, table_name),
                    )
                    columns = {row[0] for row in cur.fetchall()}
                    required = {name for name, _ in TABLE_SCHEMAS[table_name]}
                    missing = sorted(required.difference(columns))
                    cur.execute(
                        sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                            sql.Identifier(config.schema), sql.Identifier(table_name)
                        )
                    )
                    rows = int(cur.fetchone()[0])
                    cur.execute(
                        sql.SQL(
                            "SELECT COUNT(*) FROM {}.{} WHERE {} IS NULL OR {} IS NULL"
                        ).format(
                            sql.Identifier(config.schema),
                            sql.Identifier(table_name),
                            sql.Identifier(primary_keys[table_name]),
                            sql.Identifier(timestamp_columns[table_name]),
                        )
                    )
                    null_key_or_timestamp = int(cur.fetchone()[0])
                    checks = [
                        check(
                            "row_count",
                            "SUCCESS" if rows > 0 else "FAILURE",
                            "> 0",
                            rows,
                        ),
                        check(
                            "required_columns",
                            "SUCCESS" if not missing else "FAILURE",
                            sorted(required),
                            {"missing": missing},
                        ),
                        check(
                            "key_and_timestamp_not_null",
                            "SUCCESS" if null_key_or_timestamp == 0 else "FAILURE",
                            0,
                            null_key_or_timestamp,
                        ),
                    ]
                except Exception as exc:
                    checks = [
                        check(
                            "table_read",
                            "ERROR",
                            "readable PostgreSQL offline table",
                            str(exc),
                        )
                    ]
                datasets[f"postgres.feature_store.{table_name}"] = dataset_result(
                    checks
                )
    return build_validation_report("DP3", datasets)


def validate_streaming_redis() -> dict[str, Any]:
    import redis

    client = redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        decode_responses=True,
    )
    patterns = {
        "user_sequence_features": "fs:user_sequence:*",
        "user_aggregate_features": "fs:user_aggregate:*",
        "item_features": "fs:item:*",
    }
    datasets: dict[str, dict[str, Any]] = {}
    for table_name, pattern in patterns.items():
        try:
            keys = list(client.scan_iter(match=pattern, count=1000))
            sample = read_redis_payload(client, keys[0]) if keys else None
            checks = [
                check("key_count", "SUCCESS" if keys else "FAILURE", "> 0", len(keys)),
                check(
                    "payload_non_empty",
                    "SUCCESS" if sample else "FAILURE",
                    "non-empty string or hash",
                    sample,
                ),
            ]
        except Exception as exc:
            checks = [
                check(
                    "redis_read", "ERROR", f"readable keys matching {pattern}", str(exc)
                )
            ]
        datasets[f"redis.{table_name}"] = dataset_result(checks)
    return build_validation_report("STREAMING_FEATURES", datasets)


def validate_feast_online_store() -> dict[str, Any]:
    from feast import FeatureStore

    config = PostgresOfflineStoreConfig.from_env()
    repo_path = "/opt/recsys/apps/data-platform/feature-store/feature_repo"
    feature_refs = {
        "user_sequence_features": ("user_id", "hist_length"),
        "user_aggregate_features": ("user_id", "views_30m"),
        "item_features": ("product_id", "category_id"),
    }
    store = FeatureStore(repo_path=repo_path)
    datasets: dict[str, dict[str, Any]] = {}
    with config.connect() as conn:
        with conn.cursor() as cur:
            for table_name, (entity_name, feature_name) in feature_refs.items():
                try:
                    cur.execute(
                        sql.SQL(
                            "SELECT {} FROM {}.{} ORDER BY feature_timestamp DESC LIMIT 1"
                        ).format(
                            sql.Identifier(entity_name),
                            sql.Identifier(config.schema),
                            sql.Identifier(table_name),
                        )
                    )
                    entity_value = cur.fetchone()[0]
                    result = store.get_online_features(
                        features=[f"{table_name}:{feature_name}"],
                        entity_rows=[{entity_name: entity_value}],
                    ).to_dict()
                    observed = result.get(feature_name, [None])[0]
                    checks = [
                        check(
                            "native_feast_lookup",
                            "SUCCESS" if observed is not None else "FAILURE",
                            "non-null online feature",
                            observed,
                        )
                    ]
                except Exception as exc:
                    checks = [
                        check(
                            "native_feast_lookup",
                            "ERROR",
                            "readable online feature",
                            str(exc),
                        )
                    ]
                datasets[f"redis.{table_name}"] = dataset_result(checks)
    return build_validation_report("FEAST_ONLINE_STORE", datasets)


def read_redis_payload(client: Any, key: str) -> Any:
    key_type = client.type(key)
    if isinstance(key_type, bytes):
        key_type = key_type.decode("utf-8")
    if key_type == "string":
        return client.get(key)
    if key_type == "hash":
        return client.hgetall(key)
    raise ValueError(f"Unsupported Redis feature key type for {key}: {key_type}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate local dataset contracts.")
    parser.add_argument(
        "pipeline",
        choices=("dp1", "dp3-postgres", "streaming-redis", "feast-online"),
    )
    parser.add_argument("--report-uri", default="")
    parser.add_argument(
        "--run-id",
        default=os.getenv(
            "AIRFLOW_CTX_DAG_RUN_ID", os.getenv("VALIDATION_RUN_ID", "manual")
        ),
    )
    args = parser.parse_args()
    product_id = "DP1" if args.pipeline == "dp1" else "DP3"
    try:
        if args.pipeline == "dp1":
            report = validate_dp1_bronze()
        elif args.pipeline == "dp3-postgres":
            report = validate_dp3_postgres()
        elif args.pipeline == "streaming-redis":
            report = validate_streaming_redis()
        else:
            report = validate_feast_online_store()
    except Exception as exc:
        report = build_validation_report(
            product_id,
            {
                key: dataset_result(
                    [check("validation_execution", "ERROR", "completed", str(exc))]
                )
                for key in EXPECTED_DATASET_KEYS[args.pipeline]
            },
        )
    if args.report_uri:
        write_validation_report(
            validation_report(
                product_id,
                args.run_id,
                report["datasets"],
                report_uri=args.report_uri,
            ),
            args.report_uri,
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
