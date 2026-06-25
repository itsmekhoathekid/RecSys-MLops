from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class IcebergCatalogConfig:
    catalog_name: str = os.getenv("ICEBERG_CATALOG", "recsys")
    lakehouse_namespace: str = os.getenv("ICEBERG_LAKEHOUSE_NAMESPACE", "lakehouse")
    offline_feature_catalog_name: str = os.getenv("OFFLINE_FEATURE_CATALOG", "recsys_features")
    feature_namespace: str = os.getenv("ICEBERG_FEATURE_NAMESPACE", "feature_store")
    warehouse_uri: str = os.getenv("LAKEHOUSE_WAREHOUSE", "s3a://recsys-lakehouse/warehouse")
    offline_feature_warehouse_uri: str = os.getenv(
        "OFFLINE_FEATURE_STORE_WAREHOUSE",
        "s3a://recsys-offline-feature-store/warehouse",
    )
    s3_endpoint: str = os.getenv("MINIO_ENDPOINT", "http://data-platform-minio:9000")
    s3_access_key: str = os.getenv("AWS_ACCESS_KEY_ID", os.getenv("MINIO_ROOT_USER", "minio"))
    s3_secret_key: str = os.getenv("AWS_SECRET_ACCESS_KEY", os.getenv("MINIO_ROOT_PASSWORD", "minio123"))

    @property
    def lakehouse_database(self) -> str:
        return f"{self.catalog_name}.{self.lakehouse_namespace}"

    @property
    def feature_database(self) -> str:
        return f"{self.offline_feature_catalog_name}.{self.feature_namespace}"

    def lakehouse_table(self, table_name: str) -> str:
        return f"{self.lakehouse_database}.{table_name}"

    def feature_table(self, table_name: str) -> str:
        return f"{self.feature_database}.{table_name}"


FEATURE_TABLES = {
    "stream_behavior_events": "stream_behavior_events",
    "stream_user_sequence_features": "stream_user_sequence_features",
    "stream_user_aggregate_features": "stream_user_aggregate_features",
    "stream_item_features": "stream_item_features",
    "streaming_quality_windows": "streaming_quality_windows",
    "user_sequence_features": "user_sequence_features",
    "user_aggregate_features": "user_aggregate_features",
    "item_features": "item_features",
    "ml_ranking_labels": "ml_ranking_labels",
    "ml_bst_training": "ml_bst_training",
}

RAW_GENERATOR_TABLES = (
    "users",
    "user_preferences",
    "products",
    "product_snapshots",
    "sessions",
    "recommendation_requests",
    "impressions",
    "behavior_events",
    "orders",
    "order_items",
)

SILVER_LAKEHOUSE_TABLES = (
    "clean_behavior_events",
    "rejected_behavior_events",
    "clean_impressions",
    "clean_recommendation_requests",
    "order_facts",
    "product_scd",
    "users",
    "products",
    "user_preferences",
)


def _spark_catalog_conf(catalog_name: str, warehouse_uri: str) -> dict[str, str]:
    return {
        f"spark.sql.catalog.{catalog_name}": "org.apache.iceberg.spark.SparkCatalog",
        f"spark.sql.catalog.{catalog_name}.type": "hadoop",
        f"spark.sql.catalog.{catalog_name}.warehouse": warehouse_uri,
    }


def spark_iceberg_conf(config: IcebergCatalogConfig = IcebergCatalogConfig()) -> dict[str, str]:
    conf = {
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        "spark.hadoop.fs.s3a.endpoint": config.s3_endpoint,
        "spark.hadoop.fs.s3a.access.key": config.s3_access_key,
        "spark.hadoop.fs.s3a.secret.key": config.s3_secret_key,
        "spark.hadoop.fs.s3a.path.style.access": "true",
        "spark.hadoop.fs.s3a.connection.ssl.enabled": "false",
    }
    conf.update(_spark_catalog_conf(config.catalog_name, config.warehouse_uri))
    conf.update(_spark_catalog_conf(config.offline_feature_catalog_name, config.offline_feature_warehouse_uri))
    return conf


def create_spark_namespace(spark, config: IcebergCatalogConfig = IcebergCatalogConfig()) -> None:
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {config.lakehouse_database}")
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {config.feature_database}")


def create_flink_catalog_sql(
    config: IcebergCatalogConfig = IcebergCatalogConfig(),
    *,
    catalog_name: str | None = None,
    warehouse_uri: str | None = None,
) -> str:
    catalog = catalog_name or config.catalog_name
    warehouse = warehouse_uri or config.warehouse_uri
    return f"""
CREATE CATALOG {catalog} WITH (
  'type' = 'iceberg',
  'catalog-type' = 'hadoop',
  'warehouse' = '{warehouse}',
  'hadoop.fs.s3a.endpoint' = '{config.s3_endpoint}',
  'hadoop.fs.s3a.access.key' = '{config.s3_access_key}',
  'hadoop.fs.s3a.secret.key' = '{config.s3_secret_key}',
  'hadoop.fs.s3a.path.style.access' = 'true',
  'hadoop.fs.s3a.connection.ssl.enabled' = 'false',
  'property-version' = '1'
)
""".strip()
