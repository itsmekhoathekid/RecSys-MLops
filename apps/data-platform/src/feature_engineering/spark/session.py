from __future__ import annotations

import os
from typing import Any


def spark_session(app_name: str = "recsys-data-platform"):
    from pyspark.sql import SparkSession
    from lakehouse.iceberg import spark_iceberg_conf

    builder = SparkSession.builder.appName(app_name)
    builder = builder.config("spark.sql.shuffle.partitions", os.getenv("SPARK_SQL_SHUFFLE_PARTITIONS", "4"))
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
    return sanitize_columns(spark.read.parquet(f"{run_path.rstrip('/')}/{table_name}"))


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


def row_count(frame: Any) -> int:
    return int(frame.count())
