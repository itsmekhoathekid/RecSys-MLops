from __future__ import annotations

from typing import Any

from features.spark.session import read_iceberg_table, read_parquet_table, write_iceberg_table
from lakehouse.iceberg import (
    IcebergCatalogConfig,
    RAW_GENERATOR_TABLES,
    SILVER_LAKEHOUSE_TABLES,
    create_spark_namespace,
)


def _ensure_column(frame: Any, column: str, expression: Any):
    return frame if column in frame.columns else frame.withColumn(column, expression)


def build_clean_behavior_events(events: Any) -> tuple[Any, Any]:
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    normalized = _ensure_column(events, "device_type", F.lit("unknown"))
    normalized = _ensure_column(normalized, "campaign_id", F.lit("none"))
    normalized = normalized.withColumn("event_timestamp", F.to_timestamp("event_timestamp"))
    normalized = normalized.withColumn("ingestion_ts", F.to_timestamp("ingestion_ts"))
    normalized = normalized.withColumn(
        "event_type_id",
        F.when(F.col("event_type") == "view", F.lit(1))
        .when(F.col("event_type") == "cart", F.lit(2))
        .when(F.col("event_type") == "purchase", F.lit(3))
        .otherwise(F.lit(0))
        .cast("smallint"),
    )
    window = Window.partitionBy("event_id").orderBy(F.col("ingestion_ts").desc_nulls_last())
    ranked = normalized.withColumn("_dedup_rank", F.row_number().over(window))
    clean = ranked.filter(F.col("_dedup_rank") == 1).drop("_dedup_rank")
    rejected = ranked.filter(F.col("_dedup_rank") > 1).drop("_dedup_rank")
    return clean.orderBy("event_timestamp", "event_id"), rejected


def build_clean_impressions(impressions: Any) -> Any:
    from pyspark.sql import functions as F

    return (
        impressions.withColumn("impression_timestamp", F.to_timestamp("impression_timestamp"))
        .dropDuplicates(["impression_id"])
        .orderBy("impression_timestamp", "impression_id")
    )


def build_clean_recommendation_requests(requests: Any) -> Any:
    from pyspark.sql import functions as F

    frame = _ensure_column(requests, "request_context", F.lit("{}"))
    return frame.withColumn("request_timestamp", F.to_timestamp("request_timestamp"))


def build_order_facts(orders: Any, order_items: Any) -> Any:
    from pyspark.sql import functions as F

    facts = order_items.join(orders, on="order_id", how="left")
    if "status" in facts.columns:
        return facts.withColumn("is_valid_purchase", ~F.col("status").isin("cancelled", "refunded"))
    return facts.withColumn("is_valid_purchase", F.lit(True))


def build_product_scd(product_snapshots: Any, products: Any) -> Any:
    from pyspark.sql import functions as F

    if "valid_from" in product_snapshots.columns:
        return product_snapshots.withColumn("valid_from", F.to_timestamp("valid_from")).orderBy("product_id", "valid_from")
    return (
        products.withColumn("valid_from", F.to_timestamp("created_ts"))
        .withColumn("valid_to", F.lit(None).cast("timestamp"))
        .orderBy("product_id", "valid_from")
    )


def read_raw_parquet_tables(spark: Any, run_path: str) -> dict[str, Any]:
    return {table: read_parquet_table(spark, run_path, table) for table in RAW_GENERATOR_TABLES}


def read_raw_lakehouse_tables(spark: Any, catalog: IcebergCatalogConfig) -> dict[str, Any]:
    return {table: read_iceberg_table(spark, catalog.lakehouse_table(table)) for table in RAW_GENERATOR_TABLES}


def read_silver_lakehouse_tables(spark: Any, catalog: IcebergCatalogConfig) -> dict[str, Any]:
    """Read the curated DP2 outputs without rebuilding them from Bronze."""
    return {
        table: read_iceberg_table(spark, catalog.lakehouse_table(f"silver_{table}"))
        for table in SILVER_LAKEHOUSE_TABLES
    }


def build_silver_tables_from_raw(raw: dict[str, Any], catalog: IcebergCatalogConfig) -> dict[str, Any]:
    clean_events, rejected_events = build_clean_behavior_events(raw["behavior_events"])
    silver = {
        "clean_behavior_events": clean_events,
        "rejected_behavior_events": rejected_events,
        "clean_impressions": build_clean_impressions(raw["impressions"]),
        "clean_recommendation_requests": build_clean_recommendation_requests(raw["recommendation_requests"]),
        "order_facts": build_order_facts(raw["orders"], raw["order_items"]),
        "product_scd": build_product_scd(raw["product_snapshots"], raw["products"]),
        "users": raw["users"],
        "products": raw["products"],
        "user_preferences": raw["user_preferences"],
    }
    for name, frame in silver.items():
        write_iceberg_table(frame, catalog.lakehouse_table(f"silver_{name}"), mode="overwrite")
    return silver


def build_silver_tables(
    spark: Any,
    run_path: str | None = None,
    catalog: IcebergCatalogConfig | None = None,
    source: str = "lakehouse",
) -> dict[str, Any]:
    catalog = catalog or IcebergCatalogConfig()
    create_spark_namespace(spark, catalog)
    if source == "parquet":
        if run_path is None:
            raise ValueError("run_path is required when Spark batch source is parquet")
        raw = read_raw_parquet_tables(spark, run_path)
    else:
        raw = read_raw_lakehouse_tables(spark, catalog)
    return build_silver_tables_from_raw(raw, catalog)
