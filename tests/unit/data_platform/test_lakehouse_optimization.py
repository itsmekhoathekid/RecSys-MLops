from __future__ import annotations

import pytest

from features.spark.session import compact_iceberg_table
from lakehouse.iceberg import IcebergCatalogConfig
from lakehouse.optimize import ZORDER_COLUMNS, optimization_tables


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def collect(self):
        return self.rows


class FakeSpark:
    def __init__(self):
        self.queries: list[str] = []
        self.metric_reads = 0

    def sql(self, query: str):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if normalized.startswith("SELECT"):
            self.metric_reads += 1
            file_count = 8 if self.metric_reads == 1 else 2
            size = 8_000 if self.metric_reads == 1 else 7_000
            return Result(
                [
                    {
                        "file_count": file_count,
                        "total_size_bytes": size,
                        "min_file_size_bytes": 500,
                        "max_file_size_bytes": 4_000,
                        "avg_file_size_bytes": size / file_count,
                    }
                ]
            )
        if "rewrite_data_files" in normalized:
            return Result(
                [{"rewritten_data_files_count": 8, "added_data_files_count": 2}]
            )
        if "rewrite_manifests" in normalized:
            return Result(
                [{"rewritten_manifests_count": 3, "added_manifests_count": 1}]
            )
        return Result()


def test_compaction_returns_before_after_metrics_and_uses_safe_binpack_defaults():
    spark = FakeSpark()

    report = compact_iceberg_table(
        spark,
        "recsys.lakehouse.silver_users",
        target_file_size_bytes=64 * 1024 * 1024,
    )

    assert report["strategy"] == "binpack"
    assert report["before"]["file_count"] == 8
    assert report["after"]["file_count"] == 2
    assert report["rewrite_data_files"]["rewritten_data_files_count"] == 8
    queries = "\n".join(spark.queries)
    assert "'write.parquet.compression-codec' = 'zstd'" in queries
    assert "table => 'lakehouse.silver_users'" in queries
    assert "'min-input-files', '2'" in queries
    assert "rewrite_manifests" in queries


def test_compaction_generates_zorder_for_hot_query_columns():
    spark = FakeSpark()

    report = compact_iceberg_table(
        spark,
        "recsys.lakehouse.silver_clean_behavior_events",
        sort_columns=("user_id", "product_id", "event_timestamp"),
    )

    assert report["strategy"] == "zorder"
    assert "strategy => 'sort'" in "\n".join(spark.queries)
    assert "sort_order => 'zorder(user_id,product_id,event_timestamp)'" in "\n".join(
        spark.queries
    )


@pytest.mark.parametrize(
    "table_name,sort_columns",
    [
        ("recsys.lakehouse.bad-name", ()),
        ("recsys.lakehouse.table", ("user_id); DROP TABLE x",)),
    ],
)
def test_compaction_rejects_unsafe_identifiers(table_name, sort_columns):
    with pytest.raises(ValueError, match="Invalid Iceberg identifier"):
        compact_iceberg_table(FakeSpark(), table_name, sort_columns=sort_columns)


def test_repo_optimization_scope_covers_dp1_dp2_and_feature_iceberg_tables():
    tables = optimization_tables("all", IcebergCatalogConfig())

    assert len(tables) == 28
    assert "recsys.lakehouse.bronze_behavior_events" in tables
    assert "recsys.lakehouse.silver_clean_behavior_events" in tables
    assert "recsys_features.feature_store.user_sequence_features" in tables
    assert set(ZORDER_COLUMNS) <= {table.rsplit(".", 1)[-1] for table in tables}
