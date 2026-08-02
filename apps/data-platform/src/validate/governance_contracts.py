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
from metadata.datahub_validation import publish_validation_results
from metadata.governance_catalog import (
    BRONZE_URNS,
    POSTGRES_FEATURE_URNS,
    REDIS_FEATURE_URNS,
)
from metadata.runtime_lineage import RuntimeLineageRecorder


def check(name: str, status: str, expected: Any, observed: Any) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "expected": expected,
        "observed": observed,
    }


def dataset_result(checks: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = {item["status"] for item in checks}
    status = (
        "ERROR"
        if "ERROR" in statuses
        else "FAILURE"
        if "FAILURE" in statuses
        else "SUCCESS"
    )
    return {"status": status, "checks": checks}


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
    with RuntimeLineageRecorder(
        "DP1",
        "validate_stage",
        inputs=set(BRONZE_URNS.values()),
    ) as lineage:
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
                    check(
                        "row_count", "SUCCESS" if count > 0 else "FAILURE", "> 0", count
                    ),
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
                    check(
                        "table_read", "ERROR", "readable Bronze Iceberg table", str(exc)
                    )
                ]
            datasets[BRONZE_URNS[table_name]] = dataset_result(checks)
        report = publish_validation_results("DP1", datasets)
        if report["status"] == "SUCCESS":
            lineage.complete()
        else:
            lineage.fail(f"DP1 data contract status: {report['status']}")
        if owns_spark:
            spark.stop()
        return report


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
    with RuntimeLineageRecorder(
        "DP3",
        "validate_stage",
        inputs=set(POSTGRES_FEATURE_URNS.values()),
    ) as lineage:
        datasets: dict[str, dict[str, Any]] = {}
        with config.connect() as conn:
            with conn.cursor() as cur:
                for table_name, dataset_urn in POSTGRES_FEATURE_URNS.items():
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
                                sql.Identifier(config.schema),
                                sql.Identifier(table_name),
                            )
                        )
                        row_count = int(cur.fetchone()[0])
                        key = primary_keys[table_name]
                        timestamp = timestamp_columns[table_name]
                        cur.execute(
                            sql.SQL(
                                "SELECT COUNT(*) FROM {}.{} WHERE {} IS NULL OR {} IS NULL"
                            ).format(
                                sql.Identifier(config.schema),
                                sql.Identifier(table_name),
                                sql.Identifier(key),
                                sql.Identifier(timestamp),
                            )
                        )
                        null_key_or_timestamp = int(cur.fetchone()[0])
                        checks = [
                            check(
                                "row_count",
                                "SUCCESS" if row_count > 0 else "FAILURE",
                                "> 0",
                                row_count,
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
                    datasets[dataset_urn] = dataset_result(checks)
        report = publish_validation_results("DP3", datasets)
        if report["status"] == "SUCCESS":
            lineage.complete()
        else:
            lineage.fail(f"DP3 data contract status: {report['status']}")
        return report


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
        datasets[REDIS_FEATURE_URNS[table_name]] = dataset_result(checks)
    return publish_validation_results("STREAMING_FEATURES", datasets)


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
                        sql.SQL("SELECT {} FROM {}.{} ORDER BY feature_timestamp DESC LIMIT 1").format(
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
                        check("native_feast_lookup", "ERROR", "readable online feature", str(exc))
                    ]
                datasets[REDIS_FEATURE_URNS[table_name]] = dataset_result(checks)
    return publish_validation_results("FEAST_ONLINE_STORE", datasets)


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
    parser = argparse.ArgumentParser(
        description="Validate governed datasets and publish native DataHub assertion results."
    )
    parser.add_argument(
        "pipeline",
        choices=("dp1", "dp3-postgres", "streaming-redis", "feast-online"),
    )
    args = parser.parse_args()
    if args.pipeline == "dp1":
        report = validate_dp1_bronze()
    elif args.pipeline == "dp3-postgres":
        report = validate_dp3_postgres()
    elif args.pipeline == "streaming-redis":
        report = validate_streaming_redis()
    else:
        report = validate_feast_online_store()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
