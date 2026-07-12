from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any

import pyarrow.parquet as pq
from psycopg import sql

from feature_store.postgres_offline_store import TABLE_SCHEMAS, PostgresOfflineStoreConfig
from ingest.batch_lakehouse_ingestion import _filesystem_and_path
from ingest.postgres_cdc_contracts import SOURCE_TABLE_CONTRACTS
from lakehouse.iceberg import RAW_GENERATOR_TABLES
from metadata.governance_catalog import BRONZE_URNS, POSTGRES_FEATURE_URNS, REDIS_FEATURE_URNS
from metadata.runtime_lineage import RuntimeLineageRecorder


DEFAULT_REPORT_ROOT = "s3a://recsys-lakehouse/governance/validation"


def validation_run_id() -> str:
    return (
        os.getenv("VALIDATION_RUN_ID")
        or os.getenv("AIRFLOW_CTX_DAG_RUN_ID")
        or datetime.now(timezone.utc).strftime("manual-%Y%m%dT%H%M%SZ")
    )


def validation_report_root() -> str:
    return os.getenv("GOVERNANCE_VALIDATION_ROOT", DEFAULT_REPORT_ROOT).rstrip("/")


def report_uri(pipeline: str, name: str = "latest.json", *, root: str | None = None) -> str:
    return f"{(root or validation_report_root()).rstrip('/')}/{pipeline.lower()}/{name}"


def read_report(pipeline: str, *, root: str | None = None) -> dict[str, Any]:
    filesystem, path = _filesystem_and_path(report_uri(pipeline, root=root))
    with filesystem.open_input_file(path) as stream:
        return json.loads(stream.read().decode("utf-8"))


def _write_json(uri: str, payload: dict[str, Any]) -> None:
    filesystem, path = _filesystem_and_path(uri)
    parent = path.rsplit("/", 1)[0]
    filesystem.create_dir(parent, recursive=True)
    with filesystem.open_output_stream(path) as stream:
        stream.write(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))


def check(name: str, status: str, expected: Any, observed: Any) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "expected": expected,
        "observed": observed,
    }


def dataset_result(checks: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = {item["status"] for item in checks}
    status = "ERROR" if "ERROR" in statuses else "FAILURE" if "FAILURE" in statuses else "SUCCESS"
    return {"status": status, "checks": checks}


def _overall_status(datasets: dict[str, dict[str, Any]]) -> str:
    statuses = {item.get("status", "ERROR") for item in datasets.values()}
    return "ERROR" if "ERROR" in statuses else "FAILURE" if "FAILURE" in statuses else "SUCCESS"


def write_report(
    pipeline: str,
    datasets: dict[str, dict[str, Any]],
    *,
    run_id: str | None = None,
    root: str | None = None,
    merge_latest: bool = False,
) -> dict[str, Any]:
    run_id = run_id or validation_run_id()
    if merge_latest:
        try:
            previous = read_report(pipeline, root=root)
            previous_datasets = previous.get("datasets", {}) if str(previous.get("run_id")) == str(run_id) else {}
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            previous_datasets = {}
        datasets = {**previous_datasets, **datasets}
    payload = {
        "pipeline": pipeline,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": _overall_status(datasets),
        "datasets": datasets,
    }
    root_value = root or validation_report_root()
    _write_json(report_uri(pipeline, f"{run_id}.json", root=root_value), payload)
    _write_json(report_uri(pipeline, root=root_value), payload)
    return payload


def validate_dp1_bronze(*, root: str | None = None) -> dict[str, Any]:
    warehouse = os.getenv("LAKEHOUSE_WAREHOUSE", "s3a://recsys-lakehouse/warehouse").rstrip("/")
    namespace = os.getenv("ICEBERG_LAKEHOUSE_NAMESPACE", "lakehouse")
    primary_keys = {contract.table_name: contract.primary_key for contract in SOURCE_TABLE_CONTRACTS}
    with RuntimeLineageRecorder(
        "DP1",
        "validate_stage",
        inputs=set(BRONZE_URNS.values()),
        upstream_jobs={"ingest_stage"},
    ) as lineage:
        datasets: dict[str, dict[str, Any]] = {}
        for table_name in RAW_GENERATOR_TABLES:
            try:
                filesystem, path = _filesystem_and_path(f"{warehouse}/{namespace}/{table_name}")
                table = pq.read_table(path, filesystem=filesystem)
                required = set(primary_keys[table_name]) | {"source_run_id", "lakehouse_ingestion_ts"}
                missing = sorted(required.difference(table.column_names))
                checks = [
                    check("row_count", "SUCCESS" if table.num_rows > 0 else "FAILURE", "> 0", table.num_rows),
                    check("required_columns", "SUCCESS" if not missing else "FAILURE", sorted(required), {"missing": missing}),
                ]
            except Exception as exc:
                checks = [check("table_read", "ERROR", "readable bronze parquet table", str(exc))]
            datasets[BRONZE_URNS[table_name]] = dataset_result(checks)
        report = write_report("DP1", datasets, root=root)
        if report["status"] == "SUCCESS":
            lineage.complete()
        else:
            lineage.fail(f"DP1 data contract status: {report['status']}")
        return report


def validate_dp3_postgres(*, root: str | None = None) -> dict[str, Any]:
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
        upstream_jobs={"ingest_stage"},
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
                            sql.SQL("SELECT COUNT(*) FROM {}.{} WHERE {} IS NULL OR {} IS NULL").format(
                                sql.Identifier(config.schema),
                                sql.Identifier(table_name),
                                sql.Identifier(key),
                                sql.Identifier(timestamp),
                            )
                        )
                        null_key_or_timestamp = int(cur.fetchone()[0])
                        checks = [
                            check("row_count", "SUCCESS" if row_count > 0 else "FAILURE", "> 0", row_count),
                            check("required_columns", "SUCCESS" if not missing else "FAILURE", sorted(required), {"missing": missing}),
                            check(
                                "key_and_timestamp_not_null",
                                "SUCCESS" if null_key_or_timestamp == 0 else "FAILURE",
                                0,
                                null_key_or_timestamp,
                            ),
                        ]
                    except Exception as exc:
                        checks = [check("table_read", "ERROR", "readable PostgreSQL offline table", str(exc))]
                    datasets[dataset_urn] = dataset_result(checks)
        report = write_report("DP3", datasets, root=root, merge_latest=True)
        if report["status"] == "SUCCESS":
            lineage.complete()
        else:
            lineage.fail(f"DP3 data contract status: {report['status']}")
        return report


def validate_streaming_redis(*, root: str | None = None) -> dict[str, Any]:
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
            sample = client.hgetall(keys[0]) if keys else {}
            checks = [
                check("key_count", "SUCCESS" if keys else "FAILURE", "> 0", len(keys)),
                check("payload_non_empty", "SUCCESS" if sample else "FAILURE", "non-empty hash", sorted(sample)),
            ]
        except Exception as exc:
            checks = [check("redis_read", "ERROR", f"readable keys matching {pattern}", str(exc))]
        datasets[REDIS_FEATURE_URNS[table_name]] = dataset_result(checks)
    return write_report("STREAMING_FEATURES", datasets, root=root)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate governed DP1/DP3 datasets and publish DataHub contract reports.")
    parser.add_argument("pipeline", choices=("dp1", "dp3-postgres", "streaming-redis"))
    parser.add_argument("--report-root", default=None)
    args = parser.parse_args()
    if args.pipeline == "dp1":
        report = validate_dp1_bronze(root=args.report_root)
    elif args.pipeline == "dp3-postgres":
        report = validate_dp3_postgres(root=args.report_root)
    else:
        report = validate_streaming_redis(root=args.report_root)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
