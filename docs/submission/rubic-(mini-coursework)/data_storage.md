# Data Storage Optimization

This document covers the rubric rows:

- Lakehouse optimization such as compaction, partitioning, or z-order-like clustering.
- Data warehouse optimization such as secondary indexes.
- Code capture and explanation of before/after effect.

## Lakehouse Optimization

The lakehouse uses Parquet/Iceberg-style table paths on S3-compatible storage. The Spark session supports Iceberg catalogs for the raw lakehouse and offline feature store.

Code reference:

- [apps/data-platform/src/lakehouse/iceberg.py](../../../apps/data-platform/src/lakehouse/iceberg.py): Iceberg catalog configuration for lakehouse and feature-store warehouses.
- [apps/data-platform/src/ingest/batch_lakehouse_ingestion.py](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py): writes generated raw tables to lakehouse paths with deterministic `part-<run>-<table>.parquet` files.
- [apps/data-platform/src/features/spark/session.py](../../../apps/data-platform/src/features/spark/session.py): writes Iceberg tables and exposes `compact_iceberg_table`.
- [configs/local/spark_batch.yaml](../../../configs/local/spark_batch.yaml): Spark batch output warehouse and feature-store locations.

Optimization implemented:

| Technique | Implementation | Effect |
|---|---|---|
| Snappy Parquet | `pq.write_table(..., compression="snappy")` in batch lakehouse ingestion | Reduces lake storage size and scan IO versus uncompressed files. |
| Stable table layout | `LakehouseParquetLayout.table_uri()` writes one table directory per logical table | Avoids mixed-table files and makes pruning/cataloging predictable. |
| Iceberg table rewrite | `compact_iceberg_table(spark, table_name, target_file_size_bytes)` calls `rewrite_data_files` | Compacts small files into larger scan-friendly files. |
| Feature-store warehouse split | Raw lakehouse and offline feature store use separate warehouses | Keeps raw/silver/gold feature data isolated for lifecycle control. |

Run compaction from a Spark driver:

```python
from features.spark.session import compact_iceberg_table, spark_session

spark = spark_session("recsys-iceberg-compaction")
compact_iceberg_table(spark, "recsys_features.feature_store.user_sequence_features")
compact_iceberg_table(spark, "recsys_features.feature_store.user_aggregate_features")
compact_iceberg_table(spark, "recsys_features.feature_store.item_features")
```

Screenshot proof to capture:

```text
docs/pngs/lakehouse_compaction_code.png
docs/pngs/lakehouse_table_files_before_after.png
```

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

