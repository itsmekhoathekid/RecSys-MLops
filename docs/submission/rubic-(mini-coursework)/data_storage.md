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

Optimization implemented:

| Technique | Implementation | Effect |
|---|---|---|
| Bronze compression | `pq.write_table(..., compression="snappy")` in batch ingestion | Reduces raw storage and scan IO while retaining broadly compatible Parquet. |
| Iceberg bin-pack | `rewrite_data_files` with 128 MiB target and minimum 2 input files | Combines small files, reducing file-open and metadata overhead. |
| Selective Z-order | `--strategy zorder` only maps hot event/feature tables to user/item/time columns | Improves data skipping for the repository's dominant point/range access paths without paying a sort cost on every table. |
| Manifest rewrite | `rewrite_manifests` after data-file rewrite | Improves scan planning after files have moved or been regrouped. |
| Adaptive write sizing | Spark AQE coalescing with `parallelismFirst=false` and a 128 MiB advisory size | Prevents four configured shuffle partitions from automatically becoming four tiny output files for small jobs. |
| Table write properties | target size 128 MiB, hash distribution, Zstandard compression | Makes future Iceberg writes more scan-friendly; compaction also rewrites existing files using current table properties. |
| Feature-store warehouse split | Raw lakehouse and offline feature store use separate warehouses | Keeps raw/silver/gold feature data isolated for lifecycle control. |

### Why the repository does not partition by date yet

The checked-in E2E configurations generate only 1,000-2,000 entities/events. Daily partitioning at this scale would create mostly tiny partitions and increase metadata overhead. Iceberg hidden partitioning and partition evolution make it safe to add `days(event_timestamp)` later, when a table is large enough and production queries consistently filter by date. For the current workload, bin-packing plus selective clustering has the better cost/benefit ratio.

### Run and capture real before/after evidence

Safe default for weekly maintenance:

```bash
/opt/spark/bin/spark-submit \
  apps/data-platform/src/lakehouse/optimize.py \
  --scope all \
  --strategy binpack \
  --target-file-size-mb 128 \
  --min-input-files 2 \
  --skip-missing
```

For the hot query-path demonstration, run selective Z-order. Tables with no declared hot-column profile still use bin-pack:

```bash
/opt/spark/bin/spark-submit \
  apps/data-platform/src/lakehouse/optimize.py \
  --scope all \
  --strategy zorder \
  --target-file-size-mb 128 \
  --min-input-files 2 \
  --skip-missing
```

The same command is available in Airflow as `recsys_lakehouse_maintenance`. Its default Helm schedule is Sunday at 04:00 and can be made manual by setting `airflow.lakehouseMaintenanceSchedule: manual`.

The JSON output is the benchmark evidence. Capture the whole-table summary and one table detail:

```json
{
  "before_file_count": "measured from <table>.files",
  "after_file_count": "measured from <table>.files",
  "file_count_reduction": "before - after",
  "tables": [
    {
      "before": {"file_count": "...", "avg_file_size_bytes": "..."},
      "after": {"file_count": "...", "avg_file_size_bytes": "..."},
      "rewrite_data_files": {"rewritten_data_files_count": "...", "rewritten_bytes_count": "..."}
    }
  ]
}
```

Do not replace the placeholders above with an estimate. Run the job and use its emitted values; if a table already has fewer than two eligible input files, a zero rewrite count is a valid measured result.

Screenshot proof:

```text
docs/pngs/lakehouse_compaction_code.png
docs/pngs/lakehouse_table_files_before_after.png
```

Suggested analysis caption: “Before optimization, Spark task boundaries can leave multiple small files per Iceberg table. The maintenance job bin-packs eligible files toward 128 MiB, optionally clusters hot tables by user/item/time, then rewrites manifests. Compare file count and average file size from Iceberg metadata; fewer, larger files reduce open and planning overhead. On the tiny E2E dataset, scan-time improvement may be small, so the physical file metrics are the primary reproducible evidence.”

Primary references:

- [Apache Iceberg 1.7.1 Spark procedures](https://iceberg.apache.org/docs/1.7.1/spark-procedures/) documents `rewrite_data_files`, bin-pack/sort/Z-order strategies, result metrics, and `rewrite_manifests`.
- [Apache Iceberg Spark writes](https://iceberg.apache.org/docs/1.7.0/spark-writes/) explains distribution modes and why Spark task sizing constrains output file size.
- [Apache Spark 3.5.8 SQL configuration](https://spark.apache.org/docs/3.5.8/configuration.html) documents AQE advisory partition sizing and `parallelismFirst=false`.

## Data Warehouse Indexing

Source Postgres is initialized with primary keys plus secondary indexes for the query patterns used by CDC, joining, validation, feature generation, and recommendation training.

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

Verify generated DDL without touching a live database:

```bash
uv run python -c "import sys; sys.path.insert(0, 'apps/data-platform/data-generator/src'); sys.path.insert(0, 'infra/docker/scripts'); import init_postgres_schema as s; ddl=s.build_all_ddl(); print('CREATE TABLE count', ddl.count('CREATE TABLE')); print('CREATE INDEX count', ddl.count('CREATE INDEX')); print('\n'.join(ddl.splitlines()[-10:]))"
```

Observed result:

```text
CREATE TABLE count 10
CREATE INDEX count 10
CREATE INDEX IF NOT EXISTS idx_behavior_events_user_ts ON behavior_events (user_id, event_timestamp);
CREATE INDEX IF NOT EXISTS idx_behavior_events_product_ts ON behavior_events (product_id, event_timestamp);
CREATE INDEX IF NOT EXISTS idx_behavior_events_type_ts ON behavior_events (event_type, event_timestamp);
```

Image proof to capture:

```text
docs/pngs/datawarehouse_indexes_ddl.png
```
