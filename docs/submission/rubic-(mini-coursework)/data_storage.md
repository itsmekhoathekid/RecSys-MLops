# Data Storage Optimization

This implementation applies the storage rubric directly inside DP1 and DP2. There is no standalone maintenance DAG.

## Storage architecture

| Data product | Persistent input/output | Tables | Airflow order |
|---|---|---:|---|
| DP1 | Data Generator output -> Bronze Apache Iceberg | 10 `bronze_*` tables | `ingest_stage -> optimize_stage -> validate_stage` |
| DP2 | Bronze Iceberg -> curated Silver Iceberg | 9 `silver_*` tables | `ingest_stage -> optimize_stage -> validate_stage` |
| DP3 | Silver Iceberg -> offline feature Iceberg/PostgreSQL | feature tables | `ingest_stage -> validate_stage` |

MinIO/GCS-compatible object storage is the physical storage layer beneath the Iceberg Hadoop catalog. The generator still uses short-lived Parquet fragments inside the DP1 task pod, but those files are only an implementation buffer and are deleted with the pod. The first governed and persistent DP1 datasets are the Iceberg `recsys.lakehouse.bronze_*` tables.

Code references:

- [iceberg.py](../../../apps/data-platform/src/lakehouse/iceberg.py) defines both catalogs, the Bronze/Silver table inventory, and `bronze_table()` identifiers.
- [batch_lakehouse_ingestion.py](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py) reads the ephemeral generator output, adds `source_run_id` and `lakehouse_ingestion_ts`, and commits every source table through Iceberg.
- [dp2_silver_gold_entrypoint.py](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py) reads Bronze with `source="lakehouse"`; it no longer consumes a persistent Parquet layout.
- [rubric_data_pipeline_dags.py](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py) places optimization between ingest and validation for both DP1 and DP2.

## Optimization applied to both DP1 and DP2

The shared runner in [optimize.py](../../../apps/data-platform/src/lakehouse/optimize.py) supports explicit `bronze` and `silver` scopes. DP1 runs `--scope bronze --pipeline DP1`; DP2 runs `--scope silver --pipeline DP2`. Missing tables are not skipped in these governed flows, so an incomplete ingest fails before validation.

Each table receives all of the following:

| Technique | Implementation | Effect |
|---|---|---|
| Small-file compaction | Iceberg `rewrite_data_files` with `min-input-files=2` and a configurable 128 MiB target | Produces fewer, larger files and reduces file-open/object-store overhead. |
| Adaptive write sizing | Spark AQE, partition coalescing, `parallelismFirst=false`, and a 128 MiB advisory size | Reduces tiny shuffle outputs before future Iceberg writes. |
| Write distribution | `write.distribution-mode=hash` | Distributes future writes using the Iceberg table layout instead of arbitrary task order. |
| Compression | `write.parquet.compression-codec=zstd` | Reduces storage and scan I/O for Iceberg data files. |
| Manifest maintenance | Iceberg `rewrite_manifests` after data-file rewrite | Reduces fragmented metadata and scan-planning work. |
| Selective clustering | Optional Iceberg sort rewrite with `zorder(...)` for known user/item/time access paths | Improves file pruning when `LAKEHOUSE_OPTIMIZATION_STRATEGY=zorder`. |

The physical implementation is in [session.py](../../../apps/data-platform/src/features/spark/session.py). `compact_iceberg_table()` validates identifiers, captures `<table>.files` metrics before and after, persists write properties, rewrites data files, rewrites manifests, and returns a JSON-ready result.

## DP1 Bronze optimization profile

DP1 covers all ten Bronze tables. High-value clustering profiles include:

- `bronze_behavior_events`: `user_id`, `product_id`, `event_timestamp`
- `bronze_impressions`: `user_id`, `candidate_product_id`, `impression_timestamp`
- `bronze_recommendation_requests`: `user_id`, `request_timestamp`
- `bronze_sessions`: `user_id`, `session_start_ts`
- `bronze_orders`: `user_id`, `order_timestamp`
- `bronze_order_items`: `order_id`, `product_id`, `created_ts`
- `bronze_product_snapshots`: `product_id`, `valid_from`

The remaining dimension tables use bin-pack compaction because their dominant access pattern does not justify an extra sort shuffle.

## DP2 Silver optimization profile

DP2 covers all nine Silver tables. `silver_clean_behavior_events` and `silver_clean_impressions` have explicit user/item/time Z-order profiles. The other Silver tables are compacted and receive the same persistent write, compression, and manifest settings.

## Before and after

Before this change:

- DP1 persisted ordinary Parquet directories, so it could not provide Iceberg snapshots, atomic table commits, metadata tables, manifest maintenance, or Iceberg compaction.
- DP2 read those Parquet directories and only its Silver outputs were Iceberg.
- Optimization existed in a separate `recsys_lakehouse_maintenance` DAG and did not gate the DP1/DP2 data-product runs.
- The optimization inventory did not contain the ten DP1 Bronze tables.

After this change:

- DP1's first persistent datasets are ten named Bronze Iceberg tables.
- DP2 reads those tables through the Iceberg catalog.
- DP1 and DP2 each fail their own run if optimization fails.
- Validation only starts after compaction/table-property/manifest work completes.
- Runtime lineage records `optimize_stage` between `ingest_stage` and `validate_stage`.
- The standalone maintenance, composite, and duplicate batch-feature DAGs remain absent. Operational orchestration is provided separately by Feast materialization, drift/retrain, and analytics DAGs; DP1/DP2 optimization stays embedded in the authoritative rubric DAGs.

## Evidence and success criteria

For each table, the optimization report contains:

```json
{
  "table": "recsys.lakehouse.bronze_behavior_events",
  "strategy": "binpack",
  "before": {"file_count": 8, "avg_file_size_bytes": 1048576},
  "after": {"file_count": 2, "avg_file_size_bytes": 4194304},
  "rewrite_data_files": {},
  "rewrite_manifests": {}
}
```

The primary physical success signal is `after.file_count < before.file_count` together with a larger average file size. On a small E2E dataset, Iceberg may correctly report no eligible rewrite because a table already has fewer than two files. In that case, successful table-property and manifest operations still prove that the optimization stage executed without inventing a latency claim.

## Operational commands

The deployed flow is exercised in order by [cluster_data_setup.sh](../../../infra/k8s/scripts/cluster_data_setup.sh):

```bash
make cluster-data-setup
```

The underlying Spark commands are equivalent to:

```bash
spark-submit apps/data-platform/src/lakehouse/optimize.py --scope bronze --pipeline DP1
spark-submit apps/data-platform/src/lakehouse/optimize.py --scope silver --pipeline DP2
```

## Primary references

- [Apache Iceberg Spark procedures](https://iceberg.apache.org/docs/1.7.1/spark-procedures/) for `rewrite_data_files`, sort/Z-order, and `rewrite_manifests`.
- [Apache Iceberg write configuration](https://iceberg.apache.org/docs/1.7.1/configuration/#write-properties) for target file size and compression properties.
- [Apache Iceberg write distribution](https://iceberg.apache.org/docs/1.7.1/spark-writes/#writing-distribution-modes) for hash/range distribution behavior.
- [Apache Spark adaptive query execution](https://spark.apache.org/docs/3.5.8/configuration.html#adaptive-query-execution) for advisory sizing and partition coalescing.
