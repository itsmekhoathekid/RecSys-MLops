# Data Storage Optimization

This implementation applies the lakehouse optimization rubric inside DP1 and DP2. There is no standalone maintenance DAG: optimization is a required stage between ingestion and validation for both data products.

## Storage Architecture

| Data product | Persistent input/output | Tables | Airflow order |
|---|---|---:|---|
| DP1 | Data Generator -> Bronze Apache Iceberg | 10 `bronze_*` tables | `ingest_stage -> optimize_stage -> validate_stage` |
| DP2 | Bronze Iceberg -> curated Silver Iceberg | 9 `silver_*` tables | `ingest_stage -> optimize_stage -> validate_stage` |
| DP3 | Silver Iceberg -> feature Iceberg/PostgreSQL | feature tables | `ingest_stage -> validate_stage` |

MinIO or GCS-compatible object storage is the physical layer beneath the Iceberg Hadoop catalog. The generator creates short-lived Parquet fragments inside the DP1 task pod, but those fragments are deleted with the pod and are not a governed zone. The first persistent DP1 datasets are the ten `recsys.lakehouse.bronze_*` Iceberg tables.

Code references:

- Catalogs and table inventory: [iceberg.py (line 8)](../../../apps/data-platform/src/lakehouse/iceberg.py#L8).
- Parquet-fragment read and Bronze Iceberg commit: [batch_lakehouse_ingestion.py (line 69)](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L69), [line 91](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L91), and [line 97](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L97).
- DP1/DP2 optimizer commands and ordered dependencies: [rubric_data_pipeline_dags.py (line 182)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L182), [line 210](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L210), [line 246](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L246), and [line 272](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L272).

## Before Optimization

The original helper only called `rewrite_data_files` with a target file size:

```python
def compact_iceberg_table(spark, table_name, target_file_size_bytes=134_217_728):
    catalog = table_name.split(".", 1)[0]
    spark.sql(
        f"""
        CALL {catalog}.system.rewrite_data_files(
          table => '{table_name}',
          options => map('target-file-size-bytes', '{target_file_size_bytes}')
        )
        """
    )
```

Its limitations were:

1. DP1 persisted ordinary Parquet directories and therefore could not use Iceberg snapshots, metadata tables, or Iceberg procedures.
2. The helper was not a gating stage in DP1 or DP2.
3. It measured no physical files before or after the rewrite.
4. It provided no clustering profile, manifest rewrite, compression, or future-write policy.
5. The optimization inventory did not include the ten Bronze tables.

## Optimized DP1 And DP2 Flow

```text
DP1: generate -> Bronze Iceberg commit -> optimize Bronze -> validate Bronze
DP2: read Bronze Iceberg -> write Silver Iceberg -> optimize Silver -> validate Silver
```

Successful Airflow Graph runs verify this governed stage order for [DP1](data_pipeline_orchestration.md#dp1-data-generator-to-bronze-iceberg) and [DP2](data_pipeline_orchestration.md#dp2-bronze-iceberg-to-silver-iceberg).

The shared runner in [optimize.py (line 70)](../../../apps/data-platform/src/lakehouse/optimize.py#L70) executes the following steps for each selected table:

1. Resolve the governed Bronze or Silver table inventory.
2. Read Iceberg `<table>.files` metrics before mutation.
3. Persist target file size, hash distribution, and Zstandard compression properties.
4. Run `rewrite_data_files` using bin-pack by default or Z-order for configured hot paths.
5. Run `rewrite_manifests`.
6. Read the same physical-file metrics again.
7. Return a JSON-ready per-table and aggregate report.
8. Emit runtime lineage for `optimize_stage`, with the same tables as inputs and outputs and `ingest_stage` upstream.

Missing tables fail DP1/DP2 because their DAG commands do not pass `--skip-missing`. Consequently validation can never pass against a partially optimized governed table set.

## Code After Optimization

The production implementation measures the table, persists future-write properties, rewrites eligible data files, rewrites manifests, and measures the same metadata again. The following capture-focused excerpt corresponds to [session.py (line 121)](../../../apps/data-platform/src/features/spark/session.py#L121):

```python
def compact_iceberg_table(
    spark,
    table_name,
    target_file_size_bytes=134_217_728,
    *,
    min_input_files=2,
    sort_columns=(),
    rewrite_all=False,
    rewrite_manifests=True,
):
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
            f"strategy => 'sort', sort_order => 'zorder({order})',"
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

    manifest_rows = []
    if rewrite_manifests:
        manifest_rows = spark.sql(
            f"CALL {catalog}.system.rewrite_manifests("
            f"table => '{procedure_table}')"
        ).collect()

    after = iceberg_file_metrics(spark, table_name)
    return {
        "table": table_name,
        "strategy": strategy,
        "sort_columns": list(sort_columns),
        "before": before,
        "after": after,
        "rewrite_data_files": rewrite_rows,
        "rewrite_manifests": manifest_rows,
    }
```

The full function validates identifiers and positive sizing inputs before executing SQL, converts Spark result rows into JSON-ready dictionaries, and always includes the configured target size in its report. Those safety and serialization details are implemented at [session.py (line 136)](../../../apps/data-platform/src/features/spark/session.py#L136) through [line 197](../../../apps/data-platform/src/features/spark/session.py#L197).

## Detailed Before/After Analysis

| Area | Before | After | Optimization achieved |
|---|---|---|---|
| DP1 storage format | Persistent Bronze Parquet directories | Ten named Bronze Iceberg tables | Adds atomic table commits, snapshots, metadata tables, and Iceberg procedures at the first governed layer. |
| Execution | Isolated helper or detached maintenance path | Required `optimize_stage` inside DP1 and DP2 | Optimization failure blocks validation and downstream execution. |
| Small files | One generic rewrite call | Bin-pack with a 128 MiB target and minimum two eligible files | Reduces file-open overhead and object-store requests. |
| Measurement | No returned physical evidence | Reads `<table>.files` before and after every rewrite | Reports file count and total/min/max/average file size. |
| Data layout | No table-specific clustering | Optional Z-order for known user/item/time access paths | Improves Parquet/Iceberg file pruning for dominant filters. |
| Metadata | Data-file rewrite only | Data-file rewrite followed by manifest rewrite | Reduces manifest fragmentation and scan-planning work. |
| Future writes | Spark task defaults | AQE sizing, hash distribution, 128 MiB target, Zstandard | Makes later output less likely to recreate the same small-file pattern. |
| Ownership | One maintenance DAG could drift from data-product schedules | DP1 owns Bronze optimization; DP2 owns Silver optimization | Keeps lifecycle, failure, lineage, and validation inside the producing data product. |
| Safety | Missing data could be skipped by a generic maintenance run | Governed DAG commands do not pass `--skip-missing` | An incomplete Bronze or Silver inventory fails visibly. |

Query-latency improvement is intentionally not claimed from the small coursework dataset. Physical file metrics are the reproducible primary evidence; a separate controlled query benchmark is required before making a latency claim.

## Optimization Techniques

| Technique | Implementation and code reference | Effect |
|---|---|---|
| Small-file compaction | `rewrite_data_files`, 128 MiB default target, minimum two input files: [session.py (line 121)](../../../apps/data-platform/src/features/spark/session.py#L121), [line 164](../../../apps/data-platform/src/features/spark/session.py#L164), and [line 169](../../../apps/data-platform/src/features/spark/session.py#L169). | Produces fewer, larger files and reduces file-open/object-store overhead. |
| AQE write sizing | Adaptive execution, coalescing, `parallelismFirst=false`, and 128 MiB advisory sizing: [session.py (line 20)](../../../apps/data-platform/src/features/spark/session.py#L20) through [line 27](../../../apps/data-platform/src/features/spark/session.py#L27). | Reduces undersized shuffle outputs before future Iceberg writes. |
| Persistent write properties | Target file size, hash distribution, and Zstandard compression: [session.py (line 148)](../../../apps/data-platform/src/features/spark/session.py#L148). | Makes later writes less likely to recreate small files and reduces stored bytes/scan I/O. |
| Manifest maintenance | `rewrite_manifests` after the data-file rewrite: [session.py (line 180)](../../../apps/data-platform/src/features/spark/session.py#L180). | Regroups current metadata and reduces fragmented scan planning. |
| Selective Z-order | Per-table user/item/time profiles: [optimize.py (line 16)](../../../apps/data-platform/src/lakehouse/optimize.py#L16); sort strategy construction: [session.py (line 158)](../../../apps/data-platform/src/features/spark/session.py#L158). | Improves file pruning for dominant entity and time filters when `zorder` is enabled. |
| Physical evidence | Query the Iceberg `files` metadata table before and after: [session.py (line 97)](../../../apps/data-platform/src/features/spark/session.py#L97) and [line 187](../../../apps/data-platform/src/features/spark/session.py#L187). | Reports file count and min/max/average/total bytes without inventing a latency claim. |

## DP1 Bronze Optimization Profile

DP1 optimizes all ten Bronze tables. The Z-order profiles are:

| Table | Columns |
|---|---|
| `bronze_behavior_events` | `user_id`, `product_id`, `event_timestamp` |
| `bronze_impressions` | `user_id`, `candidate_product_id`, `impression_timestamp` |
| `bronze_recommendation_requests` | `user_id`, `request_timestamp` |
| `bronze_sessions` | `user_id`, `session_start_ts` |
| `bronze_orders` | `user_id`, `order_timestamp` |
| `bronze_order_items` | `order_id`, `product_id`, `created_ts` |
| `bronze_product_snapshots` | `product_id`, `valid_from` |

The remaining Bronze dimension tables use bin-pack because their dominant access path does not justify the extra sort shuffle. The inventory is selected at [optimize.py (line 34)](../../../apps/data-platform/src/lakehouse/optimize.py#L34) and the DP1 command is defined at [rubric_data_pipeline_dags.py (line 182)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L182).

## DP2 Silver Optimization Profile

DP2 optimizes all nine Silver tables. `silver_clean_behavior_events` uses `user_id`, `product_id`, and `event_timestamp`; `silver_clean_impressions` uses `user_id`, `candidate_product_id`, and `impression_timestamp`. Other Silver tables receive bin-pack compaction plus the same write, compression, and manifest policy.

The Silver inventory is selected at [optimize.py (line 38)](../../../apps/data-platform/src/lakehouse/optimize.py#L38) and the DP2 command is defined at [rubric_data_pipeline_dags.py (line 210)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L210).

## Before/After Evidence

The report schema is capture-ready:

```json
{
  "status": "SUCCESS",
  "scope": "bronze",
  "tables_optimized": 10,
  "before_file_count": 80,
  "after_file_count": 20,
  "file_count_reduction": 60,
  "tables": [
    {
      "table": "recsys.lakehouse.bronze_behavior_events",
      "strategy": "binpack",
      "before": {"file_count": 8, "avg_file_size_bytes": 1048576},
      "after": {"file_count": 2, "avg_file_size_bytes": 4194304}
    }
  ]
}
```

The primary success signal is `after.file_count < before.file_count` with a larger average file size. A small E2E dataset may already have fewer than two eligible files; in that case Iceberg can correctly perform no data-file rewrite. Successful table-property and manifest operations still prove stage execution, but they are not presented as a latency improvement.

Aggregate calculation and output are implemented at [optimize.py (line 102)](../../../apps/data-platform/src/lakehouse/optimize.py#L102) through [line 113](../../../apps/data-platform/src/lakehouse/optimize.py#L113).

## Operational Commands

Run the governed data flow, including both optimization stages:

```bash
make cluster-data-setup
```

Equivalent optimizer applications are:

```bash
spark-submit apps/data-platform/src/lakehouse/optimize.py \
  --scope bronze --pipeline DP1 --strategy binpack \
  --target-file-size-mb 128 --min-input-files 2

spark-submit apps/data-platform/src/lakehouse/optimize.py \
  --scope silver --pipeline DP2 --strategy binpack \
  --target-file-size-mb 128 --min-input-files 2
```

For selective clustering, set `LAKEHOUSE_OPTIMIZATION_STRATEGY=zorder`; tables without a configured Z-order profile continue to use bin-pack. The CLI contract is defined at [optimize.py (line 117)](../../../apps/data-platform/src/lakehouse/optimize.py#L117).

## After Optimization

- DP1's first persistent datasets are ten named Bronze Iceberg tables.
- DP2 reads Bronze through the Iceberg catalog and produces nine Silver Iceberg tables.
- Optimization failure fails the owning DP1/DP2 DAG before validation.
- Runtime lineage records `ingest_stage -> optimize_stage -> validate_stage`.
- No `recsys_lakehouse_maintenance` DAG exists; optimization cannot drift away from its data-product run.
- DP3 remains unchanged and is not given a mandatory optimization stage.

## Primary References

- [Apache Iceberg Spark procedures](https://iceberg.apache.org/docs/1.7.1/spark-procedures/) for `rewrite_data_files`, sort/Z-order, and `rewrite_manifests`.
- [Apache Iceberg write configuration](https://iceberg.apache.org/docs/1.7.1/configuration/#write-properties) for target file size and compression.
- [Apache Iceberg write distribution](https://iceberg.apache.org/docs/1.7.1/spark-writes/#writing-distribution-modes) for hash/range distribution.
- [Apache Spark adaptive query execution](https://spark.apache.org/docs/3.5.8/configuration.html#adaptive-query-execution) for advisory sizing and partition coalescing.
