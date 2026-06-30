# Schema Design

This document covers the mini-coursework documentation rows:

- Visualize tables across zones.
- Include a dimension table with SCD2 columns.
- Feature tables have `event_timestamp` and `created`/`created_timestamp` columns.
- Show relationships between dimension and fact tables.
- Document naming convention.

## Zone And Naming Convention

| Zone | Prefix/table pattern | Storage | Purpose |
|---|---|---|---|
| Source/Bronze | source table names and `raw/<run_id>/<table>` | Postgres, MinIO/S3 parquet | Raw generated OLTP/event data. |
| Silver | `silver_<clean|rejected|order|product|user>...` | Iceberg lakehouse | Cleaned, deduplicated, schema-normalized tables. |
| Gold/offline features | `user_*_features`, `item_features`, `ml_*` | Iceberg offline feature store | Training and serving feature tables. |
| Online features | `recsys:user:*`, `recsys:item:*` | Redis | Low-latency API serving features. |

Code reference:

- [apps/data-platform/data-generator/src/schemas.py](../../../apps/data-platform/data-generator/src/schemas.py): source/bronze schema definitions.
- [apps/data-platform/src/lakehouse/iceberg.py](../../../apps/data-platform/src/lakehouse/iceberg.py): raw/silver/gold table names.
- [apps/data-platform/src/features/spark/build_silver_tables.py](../../../apps/data-platform/src/features/spark/build_silver_tables.py): silver cleaning and SCD table construction.
- [apps/data-platform/src/features/flink/iceberg_feature_sink.py](../../../apps/data-platform/src/features/flink/iceberg_feature_sink.py): streaming feature table DDL.

## SCD2 Dimension

`product_snapshots` is the source SCD2-style dimension. `build_product_scd` keeps `product_id`, `valid_from`, and `valid_to`, and derives `valid_from` from `products.created_ts` if only current product rows exist.

| Column | Meaning |
|---|---|
| `product_id` | Business key. |
| `valid_from` | Start timestamp of the product snapshot. |
| `valid_to` | End timestamp of the product snapshot. `NULL` means current/open-ended row. |
| `category_id`, `brand_id`, `current_price`, `price_bucket`, `is_active` | Slowly changing attributes used by feature generation. |

## Feature Tables

| Table | Event-time column | Created column | Producer |
|---|---|---|---|
| `user_sequence_features` | `event_timestamp` | `created_timestamp` or Spark write time | Spark batch feature builder. |
| `user_aggregate_features` | `event_timestamp` | `created_timestamp` or Spark write time | Spark batch feature builder. |
| `item_features` | `event_timestamp` | `created_timestamp` or Spark write time | Spark batch feature builder. |
| `stream_user_sequence_features` | `event_timestamp` | `created_timestamp` | Flink streaming job. |
| `stream_user_aggregate_features` | `event_timestamp` | `created_timestamp` | Flink streaming job. |
| `stream_item_features` | `event_timestamp` | `created_timestamp` | Flink streaming job. |
| `streaming_quality_windows` | `window_start`, `window_end` | `created_timestamp` | Flink streaming quality-window processor. |

## Relationship Summary

```text
users.user_id
  -> sessions.user_id
  -> recommendation_requests.user_id
  -> impressions.user_id
  -> behavior_events.user_id
  -> orders.user_id

products.product_id
  -> product_snapshots.product_id
  -> impressions.candidate_product_id
  -> behavior_events.product_id
  -> order_items.product_id

recommendation_requests.request_id
  -> impressions.request_id
  -> behavior_events.request_id
```

## Run And Capture

Generate source DDL:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops
uv run python -c "import sys; sys.path.insert(0, 'apps/data-platform/data-generator/src'); sys.path.insert(0, 'infra/docker/scripts'); import init_postgres_schema as s; print(s.build_all_ddl())"
```

Check feature table code:

```bash
rg -n 'event_timestamp|created_timestamp|valid_from|valid_to|user_sequence_features|item_features' \
  apps/data-platform/src/features \
  apps/data-platform/data-generator/src/schemas.py
```

Image proof:

![Feature table timestamp columns](../../pngs/table_2_columns.png)

