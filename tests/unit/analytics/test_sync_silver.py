from __future__ import annotations

from sync_silver import AnalyticsSyncConfig, SOURCE_TABLES, spark_catalog_conf


def test_sync_config_keeps_operational_and_analytics_catalogs_isolated():
    config = AnalyticsSyncConfig()

    assert config.source_table("clean_impressions") == "recsys.lakehouse.silver_clean_impressions"
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
        "order_facts",
        "product_scd",
        "users",
    }.issubset(SOURCE_TABLES)
