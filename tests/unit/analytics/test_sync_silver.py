from __future__ import annotations

from sync_silver import AnalyticsSyncConfig, SOURCE_TABLES, redacted_config, spark_catalog_conf


def test_sync_config_keeps_operational_and_analytics_catalogs_isolated():
    config = AnalyticsSyncConfig()

    assert config.source_table("clean_impressions") == "recsys.lakehouse.silver_clean_impressions"
    assert config.source_table("orders") == "recsys.lakehouse.bronze_orders"
    assert config.source_table("order_items") == "recsys.lakehouse.bronze_order_items"
    assert config.target_table("clean_impressions") == "analytics.staging.clean_impressions"
    assert config.source_warehouse != config.target_warehouse


def test_spark_uses_hadoop_source_and_jdbc_target_catalogs():
    config = AnalyticsSyncConfig(jdbc_password="secret")
    settings = spark_catalog_conf(config)

    assert settings["spark.sql.catalog.recsys.type"] == "hadoop"
    assert settings["spark.sql.catalog.analytics.type"] == "jdbc"
    assert settings["spark.sql.catalog.analytics.uri"].startswith("jdbc:postgresql://")
    assert settings["spark.sql.catalog.analytics.jdbc.password"] == "secret"
    assert settings["spark.sql.catalog.analytics.jdbc.schema-version"] == "V1"
    assert settings["spark.hadoop.fs.s3a.path.style.access"] == "true"


def test_sync_table_set_covers_bi_source_domains():
    assert {
        "clean_behavior_events",
        "clean_impressions",
        "clean_recommendation_requests",
        "orders",
        "order_items",
        "product_scd",
        "users",
    }.issubset(SOURCE_TABLES)
    assert "order_facts" not in SOURCE_TABLES


def test_sync_config_redacts_all_credentials_from_runtime_output():
    config = AnalyticsSyncConfig(
        jdbc_password="catalog-secret",
        s3_access_key="object-store-user",
        s3_secret_key="object-store-secret",
    )

    output = redacted_config(config)

    assert output["jdbc_password"] == "***"
    assert output["s3_access_key"] == "***"
    assert output["s3_secret_key"] == "***"
    assert "catalog-secret" not in str(output)
    assert "object-store-secret" not in str(output)
