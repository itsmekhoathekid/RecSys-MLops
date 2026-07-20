# Data Storage Optimization

This document covers the rubric rows:

- Lakehouse optimization such as compaction, partitioning, or z-order-like clustering.
- Data warehouse optimization such as secondary indexes.
- Code capture and explanation of before/after effect.

## Lakehouse Optimization

The optimization scope is the DP2 Silver and DP3 offline/stream feature tables
stored as Iceberg. These curated tables benefit from file compaction,
clustering, write sizing, and manifest maintenance.

Code reference:

- [iceberg.py (line 8)](../../../apps/data-platform/src/lakehouse/iceberg.py#L8), [iceberg.py (line 113)](../../../apps/data-platform/src/lakehouse/iceberg.py#L113): Iceberg catalog configuration for lakehouse and feature-store warehouses.
- [session.py (line 11)](../../../apps/data-platform/src/features/spark/session.py#L11), [session.py (line 191)](../../../apps/data-platform/src/features/spark/session.py#L191): AQE write sizing, file metrics, compaction, optional Z-order, and manifest rewrite.
- [optimize.py (line 24)](../../../apps/data-platform/src/lakehouse/optimize.py#L24), [optimize.py (line 92)](../../../apps/data-platform/src/lakehouse/optimize.py#L92): runnable maintenance job for curated Iceberg tables.
- [rubric_data_pipeline_dags.py (line 260)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L260), [rubric_data_pipeline_dags.py (line 271)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L271): manual/cron `recsys_lakehouse_maintenance` DAG.
- [spark_batch.yaml (line 7)](../../../configs/local/spark_batch.yaml#L7), [spark_batch.yaml (line 31)](../../../configs/local/spark_batch.yaml#L31): Spark batch output warehouse and feature-store locations.

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

| Technique | Implementation | Exact code reference | How it is optimized | Official reference |
|---|---|---|---|---|
| Iceberg bin-pack compaction | Run `rewrite_data_files` with a 128 MiB target and at least two eligible input files. | [session.py (line 118)](../../../apps/data-platform/src/features/spark/session.py#L118): defaults the target to 128 MiB.<br>[session.py (line 120)](../../../apps/data-platform/src/features/spark/session.py#L120): requires at least two input files.<br>[session.py (line 153)](../../../apps/data-platform/src/features/spark/session.py#L153): selects `binpack` as the safe default.<br>[session.py (line 159)](../../../apps/data-platform/src/features/spark/session.py#L159): passes the target file size to Iceberg.<br>[session.py (line 160)](../../../apps/data-platform/src/features/spark/session.py#L160): passes the minimum input-file count.<br>[session.py (line 165)](../../../apps/data-platform/src/features/spark/session.py#L165): executes `rewrite_data_files`. | Iceberg reads several small data files, bin-packs their rows, and writes fewer larger files near the target size. This reduces file-open overhead, object-store requests, and planning metadata. A table with fewer than two eligible files is intentionally left unchanged. | [Apache Iceberg 1.7.1: `rewrite_data_files`](https://iceberg.apache.org/docs/1.7.1/spark-procedures/#rewrite_data_files) |
| Selective Z-order clustering | Use Z-order only for tables with known user/item/time access paths; all other tables remain bin-packed. | [optimize.py (line 13)](../../../apps/data-platform/src/lakehouse/optimize.py#L13): defines the per-table Z-order map.<br>[optimize.py (line 14)](../../../apps/data-platform/src/lakehouse/optimize.py#L14): clusters behavior events by user, product, and event time.<br>[optimize.py (line 16)](../../../apps/data-platform/src/lakehouse/optimize.py#L16): clusters user-sequence features by user and feature time.<br>[optimize.py (line 18)](../../../apps/data-platform/src/lakehouse/optimize.py#L18): clusters item features by product and feature time.<br>[optimize.py (line 33)](../../../apps/data-platform/src/lakehouse/optimize.py#L33): resolves sort columns for each table.<br>[session.py (line 154)](../../../apps/data-platform/src/features/spark/session.py#L154): activates the sorted rewrite only when sort columns exist.<br>[session.py (line 157)](../../../apps/data-platform/src/features/spark/session.py#L157): builds Iceberg `zorder(...)`.<br>[optimize.py (line 97)](../../../apps/data-platform/src/lakehouse/optimize.py#L97): exposes `--strategy zorder`; the CLI default remains `binpack`. | Z-order places rows with similar entity IDs and timestamps near one another across Parquet files. Iceberg/Parquet statistics can then skip more unrelated files for point and time-range filters. It is selective because sorting every table would add unnecessary shuffle and rewrite cost. | [Apache Iceberg 1.7.1: sort strategy and Z-order](https://iceberg.apache.org/docs/1.7.1/spark-procedures/#rewrite_data_files) |
| Manifest rewrite | Rewrite Iceberg manifests after data files have been compacted or clustered. | [session.py (line 175)](../../../apps/data-platform/src/features/spark/session.py#L175): checks whether manifest rewrite is enabled.<br>[session.py (line 177)](../../../apps/data-platform/src/features/spark/session.py#L177): executes `rewrite_manifests`.<br>[session.py (line 179)](../../../apps/data-platform/src/features/spark/session.py#L179): captures the manifest rewrite result. | Data-file rewrite invalidates the usefulness of old manifest grouping. Rewriting manifests regroups current file metadata, so Iceberg performs less manifest scanning and faster scan planning. | [Apache Iceberg 1.7.1: `rewrite_manifests`](https://iceberg.apache.org/docs/1.7.1/spark-procedures/#rewrite_manifests) |
| Adaptive write sizing | Enable Spark AQE partition coalescing and use a 128 MiB advisory partition size. | [session.py (line 16)](../../../apps/data-platform/src/features/spark/session.py#L16): sets the base shuffle partition count.<br>[session.py (line 17)](../../../apps/data-platform/src/features/spark/session.py#L17): enables AQE.<br>[session.py (line 18)](../../../apps/data-platform/src/features/spark/session.py#L18): enables adaptive partition coalescing.<br>[session.py (line 19)](../../../apps/data-platform/src/features/spark/session.py#L19): sets `parallelismFirst=false` so target sizing takes priority.<br>[session.py (line 21)](../../../apps/data-platform/src/features/spark/session.py#L21): configures advisory partition sizing.<br>[session.py (line 22)](../../../apps/data-platform/src/features/spark/session.py#L22): defaults the advisory size to 134,217,728 bytes. | Spark can merge undersized shuffle partitions before writing instead of blindly producing one output file per configured partition. This prevents new writes from immediately recreating many tiny files. The advisory size guides task size; it does not guarantee every physical file is exactly 128 MiB. | [Spark 3.5.8: AQE configuration](https://spark.apache.org/docs/3.5.8/configuration.html#adaptive-query-execution) and [Iceberg: controlling file sizes](https://iceberg.apache.org/docs/1.7.1/spark-writes/#controlling-file-sizes) |
| Iceberg table write properties | Persist target file size, hash distribution, and Zstandard compression on each optimized table. | [session.py (line 144)](../../../apps/data-platform/src/features/spark/session.py#L144): alters the Iceberg table properties.<br>[session.py (line 145)](../../../apps/data-platform/src/features/spark/session.py#L145): stores the target file size.<br>[session.py (line 146)](../../../apps/data-platform/src/features/spark/session.py#L146): selects hash write distribution.<br>[session.py (line 147)](../../../apps/data-platform/src/features/spark/session.py#L147): selects Zstandard Parquet compression. | These properties affect later Iceberg writes, not only the current maintenance run. The target size guides sufficiently large future writes, and Zstandard reduces curated-table storage and scan I/O. Hash distribution follows the table's Iceberg partition spec; it does not by itself cluster an unpartitioned table. | [Iceberg 1.7.1: write properties](https://iceberg.apache.org/docs/1.7.1/configuration/#write-properties) and [write distribution modes](https://iceberg.apache.org/docs/1.7.1/spark-writes/#writing-distribution-modes) |

### Measuring The Physical File Change

Measurement is evidence for the optimization, not an optimization technique.
The maintenance job reads the Iceberg `files` metadata table before and after
the rewrite, then reports file-count reduction and file-size changes.

- Code reference:
  - [session.py (line 97)](../../../apps/data-platform/src/features/spark/session.py#L97): counts physical files.
  - [session.py (line 98)](../../../apps/data-platform/src/features/spark/session.py#L98): sums physical file bytes.
  - [session.py (line 101)](../../../apps/data-platform/src/features/spark/session.py#L101): calculates average file size.
  - [session.py (line 102)](../../../apps/data-platform/src/features/spark/session.py#L102): reads the Iceberg `files` metadata table.
  - [session.py (line 140)](../../../apps/data-platform/src/features/spark/session.py#L140): captures metrics before the rewrite.
  - [session.py (line 181)](../../../apps/data-platform/src/features/spark/session.py#L181): captures metrics after the rewrite.
  - [optimize.py (line 79)](../../../apps/data-platform/src/lakehouse/optimize.py#L79): aggregates the before-file count.
  - [optimize.py (line 80)](../../../apps/data-platform/src/lakehouse/optimize.py#L80): aggregates the after-file count.
  - [optimize.py (line 89)](../../../apps/data-platform/src/lakehouse/optimize.py#L89): reports the file-count reduction.

The main success signal is fewer physical files with a larger average file
size. This document does not infer a latency improvement from the small local
dataset without a separate query benchmark.

### Running Lakehouse Maintenance

The runner and Airflow DAG make the optimization operational; they are not
physical layout techniques themselves.

- Code reference:
  - [optimize.py (line 24)](../../../apps/data-platform/src/lakehouse/optimize.py#L24): selects tables by `silver`, `features`, or `all` scope.
  - [optimize.py (line 27)](../../../apps/data-platform/src/lakehouse/optimize.py#L27): adds Silver tables.
  - [optimize.py (line 29)](../../../apps/data-platform/src/lakehouse/optimize.py#L29): adds feature tables.
  - [optimize.py (line 62)](../../../apps/data-platform/src/lakehouse/optimize.py#L62): iterates over the selected tables.
  - [optimize.py (line 65)](../../../apps/data-platform/src/lakehouse/optimize.py#L65): invokes compaction for each table.
  - [rubric_data_pipeline_dags.py (line 184)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L184): constructs the Spark maintenance command.
  - [rubric_data_pipeline_dags.py (line 186)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L186): points the command to `lakehouse/optimize.py`.
  - [rubric_data_pipeline_dags.py (line 260)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L260): defines the maintenance DAG.
  - [rubric_data_pipeline_dags.py (line 268)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L268): creates the optimization task.
  - [rubric_data_pipeline_dags.py (line 270)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L270): runs the maintenance command.

The scheduled command currently uses the CLI default `binpack`. Z-order is only
used when the job is invoked with `--strategy zorder`.

### Lakehouse And Feature Warehouse Isolation

The two Iceberg catalog roots provide storage and lifecycle isolation; this is
architecture organization, not a performance optimization technique.

- Code reference:
  - [iceberg.py (line 9)](../../../apps/data-platform/src/lakehouse/iceberg.py#L9): names the lakehouse catalog.
  - [iceberg.py (line 11)](../../../apps/data-platform/src/lakehouse/iceberg.py#L11): names the offline-feature catalog.
  - [iceberg.py (line 13)](../../../apps/data-platform/src/lakehouse/iceberg.py#L13): defines the lakehouse warehouse root.
  - [iceberg.py (line 16)](../../../apps/data-platform/src/lakehouse/iceberg.py#L16): defines the offline-feature warehouse root.
  - [iceberg.py (line 93)](../../../apps/data-platform/src/lakehouse/iceberg.py#L93): registers the lakehouse catalog with Spark.
  - [iceberg.py (line 94)](../../../apps/data-platform/src/lakehouse/iceberg.py#L94): registers the feature catalog with Spark.
  - [spark_batch.yaml (line 10)](../../../configs/local/spark_batch.yaml#L10): configures the lakehouse URI.
  - [spark_batch.yaml (line 14)](../../../configs/local/spark_batch.yaml#L14): configures the offline-feature URI.

The separation allows independent retention, ownership, governance, and
maintenance scope for curated lakehouse data and ML feature data.

### Official reference notes

All links below are primary project documentation and were checked on 2026-07-11:

- [Apache Iceberg 1.7.1 Spark procedures](https://iceberg.apache.org/docs/1.7.1/spark-procedures/) defines `rewrite_data_files`, the `binpack` and `sort` strategies, `zorder(c1,c2,...)`, rewrite result counters, and `rewrite_manifests`.
- [Apache Iceberg 1.7.1 Spark writes](https://iceberg.apache.org/docs/1.7.1/spark-writes/) explains hash/range distribution and why Spark task size constrains the resulting Iceberg file size.
- [Apache Iceberg 1.7.1 configuration](https://iceberg.apache.org/docs/1.7.1/configuration/) defines `write.target-file-size-bytes`, `write.parquet.compression-codec`, and catalog `warehouse` properties.
- [Apache Spark 3.5.8 configuration](https://spark.apache.org/docs/3.5.8/configuration.html#adaptive-query-execution) documents `spark.sql.adaptive.advisoryPartitionSizeInBytes` and `spark.sql.adaptive.coalescePartitions.parallelismFirst`.
- [Apache Iceberg 1.7.1 partition evolution](https://iceberg.apache.org/docs/1.7.1/evolution/#partition-evolution) supports the decision to defer daily partitioning at the current small scale and add or evolve a partition spec later without rewriting old data eagerly.
