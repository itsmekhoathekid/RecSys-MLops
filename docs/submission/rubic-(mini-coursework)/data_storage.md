# Data Storage Optimization

This document covers the rubric rows:

- Lakehouse optimization such as compaction, partitioning, or z-order-like clustering.
- Data warehouse optimization such as secondary indexes.
- Code capture and explanation of before/after effect.

## Lakehouse Optimization

The repository has two storage patterns:

- DP1 Bronze is immutable/run-oriented Parquet. The scheduled DP1 flow uses `overwrite`, so each table currently has one deterministic file instead of accumulating tiny append files.
- DP2 Silver and DP3 offline/stream features are Iceberg tables. These are the curated tables that benefit from file compaction, clustering, and manifest maintenance.

Code reference:

- [apps/data-platform/src/lakehouse/iceberg.py](../../../apps/data-platform/src/lakehouse/iceberg.py): Iceberg catalog configuration for lakehouse and feature-store warehouses.
- [apps/data-platform/src/ingest/batch_lakehouse_ingestion.py](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py): writes generated raw tables to lakehouse paths with deterministic `part-<run>-<table>.parquet` files.
- [apps/data-platform/src/features/spark/session.py](../../../apps/data-platform/src/features/spark/session.py): AQE write sizing, file metrics, compaction, optional Z-order, and manifest rewrite.
- [apps/data-platform/src/lakehouse/optimize.py](../../../apps/data-platform/src/lakehouse/optimize.py): runnable maintenance job for all 19 curated Iceberg tables.
- [apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py): manual/cron `recsys_lakehouse_maintenance` DAG.
- [configs/local/spark_batch.yaml](../../../configs/local/spark_batch.yaml): Spark batch output warehouse and feature-store locations.

### Code before optimization

Before this optimization, the repository only contained the following isolated helper. The normal DP2/DP3 pipelines did not call it, and it emitted no evidence about the physical files before or after the rewrite.

```python
def compact_iceberg_table(
    spark: Any,
    table_name: str,
    target_file_size_bytes: int = 134_217_728,
) -> None:
    catalog = table_name.split(".", 1)[0]
    spark.sql(
        f"""
        CALL {catalog}.system.rewrite_data_files(
          table => '{table_name}',
          options => map(
            'target-file-size-bytes',
            '{target_file_size_bytes}'
          )
        )
        """
    )
```

Limitations of the baseline:

- The helper had no production or Airflow caller, so defining it did not optimize any table automatically.
- It did not query the Iceberg `files` metadata table, so file-count or average-size improvement could not be proven.
- It only requested default bin-packing; there was no clustering strategy for user, product, or time access paths.
- It did not rewrite manifests after data files changed.
- It did not set write distribution, compression, or adaptive Spark task sizing for future writes.

### Code after optimization

The current implementation measures the table, applies write properties, rewrites eligible data files, rewrites manifests, and measures the same metadata again. This is a capture-focused excerpt from `features/spark/session.py`:

```python
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
    parts = _iceberg_identifier_parts(table_name, minimum_parts=3)
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
        strategy_arguments = (
            f"strategy => 'sort', "
            f"sort_order => 'zorder({order})',"
        )

    rewrite_rows = spark.sql(
        f"""
        CALL {catalog}.system.rewrite_data_files(
          table => '{procedure_table}',
          {strategy_arguments}
          options => map(
            'target-file-size-bytes', '{target_file_size_bytes}',
            'min-input-files', '{min_input_files}',
            'rewrite-all', '{str(rewrite_all).lower()}'
          )
        )
        """
    ).collect()
    rewrite_result = _row_dict(rewrite_rows[0]) if rewrite_rows else {}

    manifest_result = {}
    if rewrite_manifests:
        manifest_rows = spark.sql(
            f"CALL {catalog}.system.rewrite_manifests("
            f"table => '{procedure_table}')"
        ).collect()
        manifest_result = (
            _row_dict(manifest_rows[0]) if manifest_rows else {}
        )

    after = iceberg_file_metrics(spark, table_name)
    return {
        "table": table_name,
        "strategy": strategy,
        "sort_columns": list(sort_columns),
        "before": before,
        "after": after,
        "rewrite_data_files": rewrite_result,
        "rewrite_manifests": manifest_result,
    }
```

The repository-level runner applies that operation to all 9 Silver and 10 offline/stream feature tables. Z-order is only assigned to tables with a known hot access path:

```python
ZORDER_COLUMNS = {
    "silver_clean_behavior_events": (
        "user_id", "product_id", "event_timestamp"
    ),
    "silver_clean_impressions": (
        "user_id", "candidate_product_id", "impression_timestamp"
    ),
    "user_sequence_features": ("user_id", "feature_timestamp"),
    "user_aggregate_features": ("user_id", "feature_timestamp"),
    "item_features": ("product_id", "feature_timestamp"),
    "ml_ranking_labels": (
        "user_id", "candidate_product_id", "prediction_timestamp"
    ),
    "ml_bst_training": (
        "user_id", "target_item_id", "prediction_timestamp"
    ),
}

for table_name in optimization_tables(scope, catalog):
    results.append(
        compact_iceberg_table(
            spark,
            table_name,
            target_file_size_bytes,
            min_input_files=2,
            sort_columns=_sort_columns(table_name, strategy),
        )
    )
```

### Before/after analysis

| Area | Before | After | Optimization achieved |
|---|---|---|---|
| Execution | Uncalled utility function | Airflow/Spark maintenance job covers 19 Iceberg tables | Optimization is operational instead of dead code. |
| Small files | One generic rewrite call | Bin-pack with a 128 MiB target and minimum two eligible input files | Reduces the number of small files and file-open overhead. |
| Measurement | No returned result | Queries `<table>.files` before and after and returns rewrite counters | Produces reproducible evidence: file count, total bytes, and average/min/max file size. |
| Data layout | No explicit clustering | Optional Z-order for hot user/item/time columns | Improves data skipping for dominant feature and event query paths. |
| Metadata | Data files only | Data-file rewrite followed by manifest rewrite | Reduces manifest fragmentation and scan-planning work. |
| Future writes | Spark task defaults | AQE coalescing, 128 MiB advisory size, hash distribution, and Zstandard | Makes subsequent output less likely to recreate the small-file problem. |
| Safety | Arbitrary SQL identifiers and no job-level failure policy | Identifier validation; only missing tables may be skipped | Configuration or storage failures still fail the maintenance DAG visibly. |

The primary measurable success condition is `after.file_count < before.file_count` while average file size increases. Query latency is not claimed until the job is executed on the deployed lakehouse because the checked-in E2E data is intentionally small; on that scale, the physical file metrics are more stable evidence than a short wall-clock benchmark.

Optimization implemented:

| Technique | Implementation | Effect | Official reference |
|---|---|---|---|
| Bronze compression | `pq.write_table(..., compression="snappy")` in batch ingestion | Reduces raw storage and scan IO while retaining broadly compatible Parquet. | [Apache Arrow: Parquet compression](https://arrow.apache.org/docs/python/parquet.html#compression-encoding-and-file-compatibility) |
| Iceberg bin-pack | `rewrite_data_files` with 128 MiB target and minimum 2 input files | Combines small files, reducing file-open and metadata overhead. | [Apache Iceberg 1.7.1: `rewrite_data_files`](https://iceberg.apache.org/docs/1.7.1/spark-procedures/#rewrite_data_files) |
| Selective Z-order | `--strategy zorder` only maps hot event/feature tables to user/item/time columns | Improves data skipping for the repository's dominant point/range access paths without paying a sort cost on every table. | [Apache Iceberg 1.7.1: sort strategy and Z-order](https://iceberg.apache.org/docs/1.7.1/spark-procedures/#rewrite_data_files) |
| Manifest rewrite | `rewrite_manifests` after data-file rewrite | Improves scan planning after files have moved or been regrouped. | [Apache Iceberg 1.7.1: `rewrite_manifests`](https://iceberg.apache.org/docs/1.7.1/spark-procedures/#rewrite_manifests) |
| Adaptive write sizing | Spark AQE coalescing with `parallelismFirst=false` and a 128 MiB advisory size | Prevents four configured shuffle partitions from automatically becoming four tiny output files for small jobs. | [Spark 3.5.8: AQE configuration](https://spark.apache.org/docs/3.5.8/configuration.html#adaptive-query-execution) and [Iceberg: controlling file sizes](https://iceberg.apache.org/docs/1.7.1/spark-writes/#controlling-file-sizes) |
| Table write properties | target size 128 MiB, hash distribution, Zstandard compression | Makes future Iceberg writes more scan-friendly; compaction also rewrites existing files using current table properties. | [Iceberg 1.7.1: write properties](https://iceberg.apache.org/docs/1.7.1/configuration/#write-properties) and [write distribution modes](https://iceberg.apache.org/docs/1.7.1/spark-writes/#writing-distribution-modes) |
| Feature-store warehouse split | Raw lakehouse and offline feature store use separate catalog warehouse roots | Keeps raw/silver/gold feature data isolated for lifecycle control. | [Iceberg 1.7.1: catalog properties and `warehouse`](https://iceberg.apache.org/docs/1.7.1/configuration/#catalog-properties) |

### Official reference notes

All links below are primary project documentation and were checked on 2026-07-11:

- [Apache Arrow Parquet documentation](https://arrow.apache.org/docs/python/parquet.html) lists Snappy and Zstandard among supported Parquet codecs and shows the `pq.write_table(..., compression=...)` API used by Bronze ingestion.
- [Apache Iceberg 1.7.1 Spark procedures](https://iceberg.apache.org/docs/1.7.1/spark-procedures/) defines `rewrite_data_files`, the `binpack` and `sort` strategies, `zorder(c1,c2,...)`, rewrite result counters, and `rewrite_manifests`.
- [Apache Iceberg 1.7.1 Spark writes](https://iceberg.apache.org/docs/1.7.1/spark-writes/) explains hash/range distribution and why Spark task size constrains the resulting Iceberg file size.
- [Apache Iceberg 1.7.1 configuration](https://iceberg.apache.org/docs/1.7.1/configuration/) defines `write.target-file-size-bytes`, `write.parquet.compression-codec`, and catalog `warehouse` properties.
- [Apache Spark 3.5.8 configuration](https://spark.apache.org/docs/3.5.8/configuration.html#adaptive-query-execution) documents `spark.sql.adaptive.advisoryPartitionSizeInBytes` and `spark.sql.adaptive.coalescePartitions.parallelismFirst`.
- [Apache Iceberg 1.7.1 partition evolution](https://iceberg.apache.org/docs/1.7.1/evolution/#partition-evolution) supports the decision to defer daily partitioning at the current small scale and add or evolve a partition spec later without rewriting old data eagerly.

## Data Warehouse Indexing

Source Postgres is initialized with primary keys plus secondary indexes for the query patterns used by CDC, joining, validation, feature generation, and recommendation training.

Official basis: [PostgreSQL Chapter 11 — Indexes](https://www.postgresql.org/docs/current/indexes.html) explains the read-performance/write-overhead tradeoff, while [PostgreSQL multicolumn indexes](https://www.postgresql.org/docs/current/indexes-multicolumn.html) documents the composite B-tree pattern used for entity-plus-timestamp access paths below.

Code reference:

- [infra/docker/scripts/init_postgres_schema.py](../../../infra/docker/scripts/init_postgres_schema.py): source table DDL, primary keys, and secondary index DDL.
- [apps/data-platform/src/ingest/postgres_cdc_contracts.py](../../../apps/data-platform/src/ingest/postgres_cdc_contracts.py): CDC table contract and primary key/topic mapping.

Secondary indexes implemented:

| Table | Index | Query pattern |
|---|---|---|
| `product_snapshots` | `(product_id, valid_from, valid_to)` | SCD2 validity lookup by product and timestamp. |
| `sessions` | `(user_id, session_start_ts)` | User-session lookup by event time. |
| `recommendation_requests` | `(user_id, request_timestamp)` | Request history and label generation. |
| `impressions` | `(request_id, impression_timestamp)` | Request to candidates join. |
| `impressions` | `(user_id, candidate_product_id)` | User-product exposure lookup. |
| `behavior_events` | `(user_id, event_timestamp)` | User sequence features. |
| `behavior_events` | `(product_id, event_timestamp)` | Item aggregate features. |
| `behavior_events` | `(event_type, event_timestamp)` | Event type filtering for labels and quality checks. |
| `orders` | `(user_id, order_timestamp)` | Purchase labels and user aggregates. |
| `order_items` | `(product_id)` | Product conversion and order item joins. |
