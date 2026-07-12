from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import yaml

from features.spark.build_bst_training_table import build_bst_training_table
from features.spark.build_item_features import build_item_features
from features.spark.build_ranking_labels import build_ranking_labels
from features.spark.build_silver_tables import (
    build_silver_tables,
    read_silver_lakehouse_tables,
)
from features.spark.build_user_aggregate_features import build_user_aggregate_features
from features.spark.build_user_sequence_features import build_user_sequence_features
from features.spark.session import row_count, spark_session, write_iceberg_table, write_parquet
from feature_store.postgres_offline_store import (
    OFFLINE_STORE_TABLES,
    PostgresOfflineStoreConfig,
    ensure_offline_store_tables,
    insert_offline_rows,
    truncate_offline_store_tables,
)
from lakehouse.iceberg import IcebergCatalogConfig, create_spark_namespace
from metadata.governance_catalog import BRONZE_URNS, ICEBERG_FEATURE_URNS, POSTGRES_FEATURE_URNS, SILVER_URNS
from metadata.runtime_lineage import RuntimeLineageRecorder
from validate.governance_contracts import check, dataset_result, write_report


def load_config(config_path: str | Path) -> dict:
    with Path(config_path).open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def _batch_source_path(input_config: dict, output: dict) -> tuple[str, str]:
    source = input_config.get("source", os.getenv("SPARK_BATCH_SOURCE", "silver_lakehouse"))
    configured_run_path = str(input_config.get("run_path", "apps/data-platform/data-generator/src/output/test_10k_seed42"))
    if os.getenv("DATAFLOW_OUTPUT_MODE") == "s3" and not configured_run_path.startswith(("s3://", "s3a://")):
        return source, f"s3a://{output['lakehouse_bucket']}/raw/{Path(configured_run_path).name}"
    return source, configured_run_path


def _build_feature_outputs(
    silver: dict[str, Any],
    *,
    catalog: IcebergCatalogConfig,
    features: dict,
) -> dict[str, Any]:
    user_sequence = build_user_sequence_features(
        silver["clean_behavior_events"],
        max_history_length=features["max_history_length"],
    )
    user_aggregate = build_user_aggregate_features(silver["clean_behavior_events"])
    item_features = build_item_features(
        silver["clean_behavior_events"],
        silver["product_scd"],
        alpha=features["conversion_smoothing_alpha"],
        beta=features["conversion_smoothing_beta"],
    )
    labels = build_ranking_labels(
        silver["clean_impressions"],
        silver["clean_behavior_events"],
        label_window_hours=features["label_window_hours"],
    )
    training = build_bst_training_table(
        labels,
        user_sequence,
        user_aggregate,
        item_features,
        max_history_length=features["max_history_length"],
    )
    return {
        catalog.feature_table("user_sequence_features"): user_sequence,
        catalog.feature_table("user_aggregate_features"): user_aggregate,
        catalog.feature_table("item_features"): item_features,
        catalog.feature_table("ml_ranking_labels"): labels,
        catalog.feature_table("ml_bst_training"): training,
    }


def _output_summary(outputs: dict[str, Any]) -> dict[str, int]:
    return {table_name.rsplit(".", 1)[-1]: row_count(frame) for table_name, frame in outputs.items()}


def _is_enabled(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _postgres_export_config(output: dict[str, Any]) -> dict[str, Any]:
    config = output.get("feast_postgres_export", {})
    enabled = config.get("enabled", output.get("feast_postgres_export_enabled", os.getenv("FEAST_POSTGRES_EXPORT_ENABLED", "0")))
    return {
        "enabled": _is_enabled(enabled),
        "config": PostgresOfflineStoreConfig.from_output(output),
    }


def _write_postgres_tables(outputs: dict[str, Any], *, catalog: IcebergCatalogConfig, output: dict[str, Any]) -> tuple[str, ...]:
    export = _postgres_export_config(output)
    if not export["enabled"]:
        return ()
    config: PostgresOfflineStoreConfig = export["config"]
    with config.connect() as conn:
        ensure_offline_store_tables(conn, config.schema, OFFLINE_STORE_TABLES)
        truncate_offline_store_tables(conn, config.schema, OFFLINE_STORE_TABLES)
        for table_name in OFFLINE_STORE_TABLES:
            frame = outputs[catalog.feature_table(table_name)].toPandas()
            insert_offline_rows(conn, config.schema, table_name, frame.to_dict("records"))
    return tuple(POSTGRES_FEATURE_URNS[table_name] for table_name in OFFLINE_STORE_TABLES)


def _write_dp3_iceberg_validation_report(outputs: dict[str, Any], *, catalog: IcebergCatalogConfig) -> dict[str, Any]:
    from pyspark.sql import functions as F

    required_columns = {
        "user_sequence_features": {"user_id", "feature_timestamp"},
        "user_aggregate_features": {"user_id", "feature_timestamp"},
        "item_features": {"product_id", "feature_timestamp"},
        "ml_ranking_labels": {"impression_id", "prediction_timestamp"},
        "ml_bst_training": {"impression_id", "prediction_timestamp"},
    }
    datasets: dict[str, dict[str, Any]] = {}
    for table_name, required in required_columns.items():
        frame = outputs[catalog.feature_table(table_name)]
        observed_rows = row_count(frame)
        missing = sorted(required.difference(frame.columns))
        null_keys = None
        if not missing:
            null_expression = None
            for column in sorted(required):
                expression = F.col(column).isNull()
                null_expression = expression if null_expression is None else null_expression | expression
            null_keys = frame.filter(null_expression).count()
        checks = [
            check("row_count", "SUCCESS" if observed_rows > 0 else "FAILURE", "> 0", observed_rows),
            check("required_columns", "SUCCESS" if not missing else "FAILURE", sorted(required), {"missing": missing}),
            check(
                "key_and_timestamp_not_null",
                "ERROR" if null_keys is None else "SUCCESS" if null_keys == 0 else "FAILURE",
                0,
                null_keys,
            ),
        ]
        datasets[ICEBERG_FEATURE_URNS[table_name]] = dataset_result(checks)
    return write_report("DP3", datasets)


def run_pyspark_batch(config_path: str | Path = "configs/local/spark_batch.yaml") -> dict[str, int]:
    spark = spark_session("recsys-pyspark-batch-features")
    config = load_config(config_path)
    input_config = config.get("input", {})
    output = config["output"]
    features = config["features"]
    catalog = IcebergCatalogConfig(
        catalog_name=output.get("iceberg_catalog", os.getenv("ICEBERG_CATALOG", "recsys")),
        lakehouse_namespace=output.get(
            "iceberg_lakehouse_namespace",
            os.getenv("ICEBERG_LAKEHOUSE_NAMESPACE", "lakehouse"),
        ),
        offline_feature_catalog_name=output.get(
            "offline_feature_catalog",
            os.getenv("OFFLINE_FEATURE_CATALOG", "recsys_features"),
        ),
        feature_namespace=output.get("iceberg_feature_namespace", os.getenv("ICEBERG_FEATURE_NAMESPACE", "feature_store")),
        warehouse_uri=output.get("lakehouse_warehouse", os.getenv("LAKEHOUSE_WAREHOUSE", "s3a://recsys-lakehouse/warehouse")),
        offline_feature_warehouse_uri=output.get(
            "offline_feature_store_warehouse",
            os.getenv("OFFLINE_FEATURE_STORE_WAREHOUSE", "s3a://recsys-offline-feature-store/warehouse"),
        ),
    )
    create_spark_namespace(spark, catalog)
    source, run_path = _batch_source_path(input_config, output)

    try:
        with RuntimeLineageRecorder("DP3", "ingest_stage") as lineage:
            if source == "silver_lakehouse":
                silver = read_silver_lakehouse_tables(spark, catalog)
                lineage.add_inputs(*SILVER_URNS.values())
            else:
                silver = build_silver_tables(spark, run_path=run_path, catalog=catalog, source=source)
                lineage.add_inputs(*BRONZE_URNS.values())
            outputs = _build_feature_outputs(silver, catalog=catalog, features=features)
            for table_name, frame in outputs.items():
                write_iceberg_table(frame, table_name, mode="overwrite")
                lineage.add_outputs(ICEBERG_FEATURE_URNS[table_name.rsplit(".", 1)[-1]])
            feast_offline_root = output.get("feast_offline_store_uri")
            if feast_offline_root:
                for table_name in ("user_sequence_features", "user_aggregate_features", "item_features"):
                    write_parquet(
                        outputs[catalog.feature_table(table_name)],
                        f"{feast_offline_root.rstrip('/')}/{table_name}",
                    )
            lineage.add_outputs(*_write_postgres_tables(outputs, catalog=catalog, output=output))
            report = _write_dp3_iceberg_validation_report(outputs, catalog=catalog)
            if report["status"] != "SUCCESS":
                lineage.fail(f"DP3 Iceberg data contract status: {report['status']}")
                raise AssertionError(f"DP3 Iceberg validation failed: {report}")
            summary = {"clean_behavior_events": row_count(silver["clean_behavior_events"])}
            summary.update(_output_summary(outputs))
            return summary
    finally:
        spark.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PySpark batch feature flow")
    parser.add_argument("--config", default="configs/local/spark_batch.yaml")
    args = parser.parse_args()
    print(json.dumps(run_pyspark_batch(args.config), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
