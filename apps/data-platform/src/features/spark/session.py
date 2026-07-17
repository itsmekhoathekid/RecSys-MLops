from __future__ import annotations

import os
import re
from typing import Any


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_or_default(name: str, default: str) -> str:
    return os.getenv(name) or default


def spark_session(app_name: str = "recsys-data-platform"):
    from pyspark.sql import SparkSession
    from lakehouse.iceberg import spark_iceberg_conf

    builder = SparkSession.builder.appName(app_name)
    builder = builder.config("spark.sql.shuffle.partitions", os.getenv("SPARK_SQL_SHUFFLE_PARTITIONS", "4"))
    builder = builder.config("spark.sql.adaptive.enabled", "true")
    builder = builder.config("spark.sql.adaptive.coalescePartitions.enabled", "true")
    builder = builder.config("spark.sql.adaptive.coalescePartitions.parallelismFirst", "false")
    builder = builder.config("spark.sql.parquet.mergeSchema", "true")
    builder = builder.config(
        "spark.sql.adaptive.advisoryPartitionSizeInBytes",
        _env_or_default("SPARK_ADVISORY_PARTITION_SIZE_BYTES", "134217728"),
    )
    for key, value in spark_iceberg_conf().items():
        builder = builder.config(key, value)
    if os.getenv("SPARK_MASTER"):
        builder = builder.master(os.environ["SPARK_MASTER"])
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel(os.getenv("SPARK_LOG_LEVEL", "WARN"))
    return spark


def sanitize_columns(frame: Any):
    seen: set[str] = set()
    renamed_columns: list[str] = []
    duplicate_columns: list[str] = []
    for index, column in enumerate(frame.columns):
        key = column.lower()
        if key in seen:
            duplicate_name = f"__duplicate_{index}_{column}"
            renamed_columns.append(duplicate_name)
            duplicate_columns.append(duplicate_name)
        else:
            seen.add(key)
            renamed_columns.append(column)
    if duplicate_columns:
        frame = frame.toDF(*renamed_columns).drop(*duplicate_columns)
    if "business_date" in frame.columns:
        frame = frame.drop("business_date")
    return frame


def read_parquet_table(spark: Any, run_path: str, table_name: str):
    frame = spark.read.option("mergeSchema", "true").parquet(f"{run_path.rstrip('/')}/{table_name}")
    return sanitize_columns(frame)


def read_iceberg_table(spark: Any, table_name: str):
    return sanitize_columns(spark.table(table_name))


def write_parquet(frame: Any, output_path: str) -> None:
    sanitize_columns(frame).write.mode("overwrite").parquet(output_path)


def write_iceberg_table(frame: Any, table_name: str, mode: str = "append") -> None:
    writer = sanitize_columns(frame).writeTo(table_name)
    if mode == "overwrite":
        writer.createOrReplace()
        return
    try:
        writer.append()
    except Exception:
        writer.create()


def _iceberg_identifier_parts(identifier: str, *, minimum_parts: int = 1) -> tuple[str, ...]:
    parts = tuple(identifier.split("."))
    if len(parts) < minimum_parts or any(not _IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError(f"Invalid Iceberg identifier: {identifier}")
    return parts


def _row_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "asDict"):
        return dict(row.asDict(recursive=True))
    if isinstance(row, dict):
        return dict(row)
    return {}


def iceberg_file_metrics(spark: Any, table_name: str) -> dict[str, int | float]:
    """Read physical file metrics from an Iceberg metadata table."""
    _iceberg_identifier_parts(table_name, minimum_parts=3)
    rows = spark.sql(
        f"""
        SELECT
          COUNT(*) AS file_count,
          COALESCE(SUM(file_size_in_bytes), 0) AS total_size_bytes,
          COALESCE(MIN(file_size_in_bytes), 0) AS min_file_size_bytes,
          COALESCE(MAX(file_size_in_bytes), 0) AS max_file_size_bytes,
          COALESCE(AVG(file_size_in_bytes), 0) AS avg_file_size_bytes
        FROM {table_name}.files
        """
    ).collect()
    row = _row_dict(rows[0]) if rows else {}
    return {
        "file_count": int(row.get("file_count") or 0),
        "total_size_bytes": int(row.get("total_size_bytes") or 0),
        "min_file_size_bytes": int(row.get("min_file_size_bytes") or 0),
        "max_file_size_bytes": int(row.get("max_file_size_bytes") or 0),
        "avg_file_size_bytes": float(row.get("avg_file_size_bytes") or 0),
    }


def compact_iceberg_table(
    spark: Any,
    table_name: str,
    target_file_size_bytes: int = 134_217_728,
    *,
    min_input_files: int = 2,
    sort_columns: tuple[str, ...] = (),
    rewrite_all: bool = False,
    rewrite_manifests: bool = True,
) -> dict[str, Any]:
    """Compact an Iceberg table and return capture-ready before/after evidence.

    Bin-packing is the safe default. Passing ``sort_columns`` changes the Iceberg
    rewrite strategy to sort with a Z-order expression for hot query columns.
    """
    parts = _iceberg_identifier_parts(table_name, minimum_parts=3)
    if target_file_size_bytes <= 0:
        raise ValueError("target_file_size_bytes must be positive")
    if min_input_files < 1:
        raise ValueError("min_input_files must be at least 1")
    for column in sort_columns:
        _iceberg_identifier_parts(column)

    catalog = parts[0]
    procedure_table = ".".join(parts[1:])
    before = iceberg_file_metrics(spark, table_name)

    spark.sql(
        f"""
        ALTER TABLE {table_name} SET TBLPROPERTIES (
          'write.target-file-size-bytes' = '{target_file_size_bytes}',
          'write.distribution-mode' = 'hash',
          'write.parquet.compression-codec' = 'zstd'
        )
        """
    )

    strategy_arguments = ""
    strategy = "binpack"
    if sort_columns:
        strategy = "zorder"
        order = ",".join(sort_columns)
        strategy_arguments = f"strategy => 'sort', sort_order => 'zorder({order})',"
    options = (
        f"'target-file-size-bytes', '{target_file_size_bytes}', "
        f"'min-input-files', '{min_input_files}', "
        f"'rewrite-all', '{str(rewrite_all).lower()}'"
    )
    rewrite_rows = spark.sql(
        f"""
        CALL {catalog}.system.rewrite_data_files(
          table => '{procedure_table}',
          {strategy_arguments}
          options => map({options})
        )
        """
    ).collect()
    rewrite_result = _row_dict(rewrite_rows[0]) if rewrite_rows else {}

    manifest_result: dict[str, Any] = {}
    if rewrite_manifests:
        manifest_rows = spark.sql(
            f"CALL {catalog}.system.rewrite_manifests(table => '{procedure_table}')"
        ).collect()
        manifest_result = _row_dict(manifest_rows[0]) if manifest_rows else {}

    after = iceberg_file_metrics(spark, table_name)
    return {
        "table": table_name,
        "strategy": strategy,
        "sort_columns": list(sort_columns),
        "target_file_size_bytes": target_file_size_bytes,
        "before": before,
        "after": after,
        "rewrite_data_files": rewrite_result,
        "rewrite_manifests": manifest_result,
    }


def row_count(frame: Any) -> int:
    return int(frame.count())
