# Processing Jobs

This page documents the current runtime proof plan for Spark batch processing and Flink stream processing. The proof is based on the real data generator, lakehouse input, Kafka CDC stream, Spark UI, Flink UI, and feature-store outputs.

## Current Data Generator Data Problems Config

### Batch generator for lakehouse data

The batch generator writes raw recommendation-system data into the lakehouse with data issues turned on, so the Spark batch job can process realistic offline data problems before exporting features to the Feast PostgreSQL offline store.

Code reference:

- [configs/local/data_generator_e2e_1k.yaml line 5](../../../configs/local/data_generator_e2e_1k.yaml#L5): batch entity volume for high-cardinality proof.
- [configs/local/data_generator_e2e_1k.yaml line 27](../../../configs/local/data_generator_e2e_1k.yaml#L27): skewed distribution knobs.
- [configs/local/data_generator_e2e_1k.yaml line 41](../../../configs/local/data_generator_e2e_1k.yaml#L41): duplicate, late-arrival, and out-of-order injection rates.
- [configs/local/data_generator_e2e_1k.yaml line 57](../../../configs/local/data_generator_e2e_1k.yaml#L57): schema evolution and breaking-schema config.

The current config is intentionally stress-heavy. It uses a large entity space (`20,000` products, `8,000` users, `5,000` brands, `1,000` categories) so Spark can show high-cardinality aggregations. It also makes the category and city distributions very uneven (`top_category_ratio=0.99`, `top_city_ratio=0.96`), which creates a hot-key skew pattern around the dominant category. Duplicate issues are injected with `duplicate_event_rate=0.45` and `conflicting_duplicate_rate=0.18`, so the silver layer has to reject repeated `event_id` rows before writing offline features. Schema evolution is enabled with a compatible cutover on `2026-03-23` and a breaking v3 schema after `2026-03-27`, so the normal job can count breaking rows and the fail-proof job can demonstrate what happens when unsupported schema reaches Spark. Late and out-of-order settings are also enabled for the Flink proof path.

### Streaming generator for Kafka CDC and Flink jobs

The realtime producer continuously inserts source rows into PostgreSQL. CDC then sends behavior events to Kafka topic `cdc.behavior_events`, where the two continuous Flink jobs consume them:

- Flink offline-store job writes processed streaming features to the Feast PostgreSQL offline store.
- Flink online-store job writes online features to Redis.

Code reference:

- [infra/helm/recsys-data-platform/values.yaml line 125](../../../infra/helm/recsys-data-platform/values.yaml#L125): realtime producer configuration.
- [infra/helm/recsys-data-platform/values.yaml line 133](../../../infra/helm/recsys-data-platform/values.yaml#L133): normal realtime event rate.
- [infra/helm/recsys-data-platform/values.yaml line 138](../../../infra/helm/recsys-data-platform/values.yaml#L138): hot-product, duplicate, late, and out-of-order stress settings.
- [infra/helm/recsys-data-platform/values.yaml line 146](../../../infra/helm/recsys-data-platform/values.yaml#L146): burst interval and multiplier.
- [apps/data-platform/data-generator/src/scripts/run_realtime_postgres_producer.py line 333](../../../apps/data-platform/data-generator/src/scripts/run_realtime_postgres_producer.py#L333): runtime emission logic for burst/duplicate/late events.

The streaming config is tuned to make Flink runtime issues visible without turning on the full ML system. A normal producer tick emits `40` events, while every 5th tick is multiplied by `8`, creating `320`-event burst windows. Hot-product skew is produced with `hotProductRatio=0.70` across `3` hot products. Duplicate and conflicting duplicate events are replayed into the CDC stream, and late/out-of-order timestamps are emitted so Flink watermark, window, dedup, throughput, checkpoint, and backpressure behavior can be captured from the Flink UI and quality windows.

## Spark Job To Handle Offline Data Problems

The Spark batch job reads raw tables from the data lakehouse, normalizes/deduplicates them into silver tables, computes offline feature tables, writes Iceberg feature tables, and exports to the Feast PostgreSQL offline feature store.

Code reference:

- [apps/data-platform/src/features/spark/spark_batch_entrypoint.py line 111](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L111): production Spark batch entrypoint.
- [apps/data-platform/src/features/spark/spark_batch_entrypoint.py line 138](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L138): builds normalized silver tables from lakehouse input.
- [apps/data-platform/src/features/spark/spark_batch_entrypoint.py line 139](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L139): builds offline feature outputs.
- [apps/data-platform/src/features/spark/spark_batch_entrypoint.py line 149](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L149): exports batch feature tables into PostgreSQL Feast offline store.

#### Skew Problems

**Spark UI navigation**

1. Open `SQL / DataFrame`.
2. Open `DP3 HEAVY SQL - skewed category_id aggregation with 32 shuffle tasks`.
3. Use the description as the stable lookup key. The numeric SQL id changes every rerun.
4. Capture the SQL DAG where `Generate`, `Expand`, `Exchange`, and `HashAggregate` are visible.
5. Open the associated job/stage from that SQL execution.
6. Capture the stage `Event Timeline`, `Summary Metrics`, and task table. Focus on `Shuffle Read Size / Records`, `Shuffle Write Size / Records`, and the difference between median and max task duration.

![Spark baseline skew problem](../../pngs/spark_baseline_skew_problem.png)

**Figure: Spark stage-level skew proof.** This screenshot shows the skew proof stage with `32 completed tasks`, `Shuffle Read Size / Records = 1856.9 KiB / 30751`, and `Shuffle Write Size / Records = 1593.3 KiB / 51752`. The important part is the task-level comparison: the stage is no longer a tiny single-task check, so the reviewer can compare task duration and shuffle records across 32 tasks. In this capture, max task duration is `0.2 s` while the median is `28 ms`, which is the kind of imbalance Spark UI exposes when a hot key creates uneven work.

Reference Spark SQL code here:

[infra/k8s/processing-baseline/spark-baseline-ui-job.yaml line 161](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L161) defines the heavy skew SQL used by the Spark UI proof. The core line is the `CASE WHEN category_id = 1 THEN 24 ELSE 2 END` multiplier: rows from the hot category are expanded 24 times, while other categories are expanded only 2 times. This keeps the proof deterministic and makes the skew obvious in one SQL execution with 32 shuffle tasks.

```sql
WITH amplified AS (
  SELECT
    category_id,
    product_id,
    event_id,
    user_id,
    CAST(price AS DOUBLE) AS price,
    repeat_id
  FROM clean_behavior_events_proof
  LATERAL VIEW explode(
    sequence(
      1,
      CASE WHEN category_id = 1 THEN 24 ELSE 2 END
    )
  ) repeat_view AS repeat_id
)
SELECT
  category_id,
  COUNT(*) AS amplified_event_rows,
  COUNT(DISTINCT event_id) AS source_event_count,
  COUNT(DISTINCT product_id) AS product_cardinality_inside_category,
  SUM(price) AS amplified_price_sum
FROM amplified
GROUP BY category_id
ORDER BY amplified_event_rows DESC
LIMIT 20
```

**Spark SQL note:** the generator config creates the real hot category distribution, then this proof query amplifies that hot key so the Spark UI shows a visible heavy aggregation. The `GROUP BY category_id` forces a category-key aggregation, and the proof wrapper runs it with `spark.sql.shuffle.partitions=32` and AQE disabled so Spark exposes multiple comparable tasks instead of coalescing them away.

![Spark baseline skew problem](../../pngs/skew_spark_sql_ui.png)

**Figure: Spark SQL DAG proof for skew amplification.** This screenshot shows the SQL DAG path used by the skew proof: `Generate` outputs `736,572` rows, `Expand` outputs `2,209,716` rows, then `HashAggregate` groups the expanded data and reports `51,752` output rows. The `HashAggregate` node also shows aggregation build time and peak memory, which proves this is a real Spark SQL aggregation path rather than a simple printed log.

**What to point out in the screenshots:** the Spark SQL DAG proves the query shape (`Generate -> Expand -> HashAggregate`), while the Spark stage screenshot proves that Spark executed it as a multi-task shuffle/aggregation stage. The CLI summary can be captured separately to show the business-level hot key: `category_id=1` has the largest `amplified_event_rows`, so the Spark UI evidence can be tied back to a concrete skewed category.

**Analysis:** this is the baseline data-skew proof for the lakehouse-to-offline-store Spark path. The data generator creates the skew through `top_category_ratio=0.99`, and the heavy SQL query makes that skew visible in Spark UI by expanding the dominant category and grouping by `category_id`. The proof is stronger than the old count-only query because one SQL execution now has enough rows and enough shuffle tasks to compare task-level behavior.

Code reference:

- [configs/local/data_generator_e2e_1k.yaml line 27](../../../configs/local/data_generator_e2e_1k.yaml#L27): skewed category and city distribution config.
- [infra/k8s/processing-baseline/spark-baseline-ui-job.yaml line 161](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L161): heavy skew SQL with hot-category amplification.

#### High Cardinality

**Spark UI navigation**

1. Open `SQL / DataFrame`.
2. Open `DP3 HEAVY SQL - high-cardinality product_event_key aggregation with 32 shuffle tasks`.
3. Use the description as the stable lookup key. The numeric SQL id changes every rerun.
4. Capture the SQL DAG where `Generate`, `Expand`, `Exchange`, and `HashAggregate` are visible.
5. Open the associated job/stage from that SQL execution.
6. Capture the stage `Event Timeline`, `Summary Metrics`, and task table. Focus on `Shuffle Read Size / Records`, the number of completed tasks, and task duration spread.

![Spark high cardinality runtime proof](../../pngs/high_cardinality_metrics.png)

**Figure: Spark stage-level high-cardinality proof.** This screenshot shows the high-cardinality proof stage with `32 completed tasks`, `Shuffle Read Size / Records = 1792.1 KiB / 30751`, and associated job `53`. The stage view is useful because it proves the query ran as a real multi-task Spark shuffle stage, not as a small driver-only count. The task timeline and summary metrics let the reviewer compare how many records Spark had to shuffle and how evenly those records were processed across tasks.

Reference Spark SQL code here:

[infra/k8s/processing-baseline/spark-baseline-ui-job.yaml line 189](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L189) defines the heavy high-cardinality SQL used by the Spark UI proof. The important field is `product_event_key`, which combines `product_id`, `event_id`, and `repeat_id` so Spark has to aggregate many near-unique keys. The query also includes both exact distinct counting and `approx_count_distinct(product_event_key, 0.05)` to show the optimized estimator in the same SQL path.

```sql
WITH amplified AS (
  SELECT
    product_id,
    event_id,
    user_id,
    category_id,
    repeat_id,
    CONCAT(
      CAST(product_id AS STRING),
      ':',
      event_id,
      ':',
      CAST(repeat_id AS STRING)
    ) AS product_event_key
  FROM clean_behavior_events_proof
  LATERAL VIEW explode(sequence(1, 8)) repeat_view AS repeat_id
)
SELECT
  product_id,
  COUNT(*) AS amplified_rows,
  COUNT(DISTINCT product_event_key) AS exact_high_cardinality_keys,
  approx_count_distinct(product_event_key, 0.05) AS approx_high_cardinality_keys,
  COUNT(DISTINCT user_id) AS user_cardinality_per_product
FROM amplified
GROUP BY product_id
ORDER BY exact_high_cardinality_keys DESC, product_id ASC
LIMIT 100
```

**Spark SQL note:** the generator config already creates a large entity space with `20,000` products and `8,000` users. This proof query makes the high-cardinality pressure obvious in Spark UI by expanding each behavior event 8 times and creating a near-unique `product_event_key`. The `GROUP BY product_id` plus `COUNT(DISTINCT product_event_key)` forces Spark to maintain many distinct keys, while `approx_count_distinct(product_event_key, 0.05)` shows the approximate estimator that can be used when exact cardinality is too expensive.

![Spark high cardinality runtime proof](../../pngs/high_cardinality_spark_sql.png)

**Figure: Spark SQL DAG proof for high cardinality.** This screenshot shows the DAG generated by the heavy high-cardinality SQL. `Generate` outputs `246,008` rows, `Expand` outputs `738,024` rows, and the downstream `HashAggregate` reports `289,053` output rows. The same `HashAggregate` node shows aggregation build time, peak memory, and hash probe metrics, which are the Spark UI signals that this query is doing substantial distinct-key aggregation work.

**What to point out in the screenshots:** the SQL DAG proves the query shape (`Generate -> Expand -> HashAggregate`) and the large output-row counts produced by distinct-key aggregation. The stage screenshot proves Spark executed the query with 32 tasks and shuffle metrics. Together, they show high cardinality as a physical Spark workload: many unique keys flow through an aggregation and shuffle boundary, instead of only being described in text.

**Analysis:** high cardinality means Spark must process many distinct business keys. The stress generator creates the raw entity space, then the heavy SQL makes the pressure visible by creating `product_event_key` values that are close to unique per event expansion. The exact `COUNT(DISTINCT product_event_key)` is the baseline pressure point, while `approx_count_distinct(product_event_key, 0.05)` is the lightweight estimator proof when the pipeline needs a cardinality signal without fully materializing every distinct key.

Code reference:

- [configs/local/data_generator_e2e_1k.yaml line 5](../../../configs/local/data_generator_e2e_1k.yaml#L5): high-cardinality entity counts.
- [infra/k8s/processing-baseline/spark-baseline-ui-job.yaml line 189](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L189): heavy high-cardinality SQL definition.
- [infra/k8s/processing-baseline/spark-baseline-ui-job.yaml line 205](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L205): approximate distinct estimator with `approx_count_distinct(..., 0.05)`.

#### Schema Evolution

**Failure-proof capture command**

```bash
kubectl apply -f infra/k8s/processing-baseline/spark-schema-evolution-fail-job.yaml
kubectl wait --for=condition=failed job/spark-schema-evolution-fail-proof -n recsys-dataflow --timeout=5m
kubectl logs -n recsys-dataflow job/spark-schema-evolution-fail-proof
```

Capture these log lines:

```text
ValueError: unsupported behavior_events schema_version=3
Task 0 in stage 13.0 failed 1 times; aborting job
```

**Image proof: Spark UI counts breaking schema rows before normalization**

![Spark UI schema evolution proof - breaking schema_version rows before normalization](../../pngs/schema_evolution_proof.png)

**Figure: Spark UI schema-evolution proof from `docs/pngs/schema_evolution_proof.png`.** This image should show the Spark SQL/DataFrame execution labelled `DP3 CHECK - count breaking schema_version rows before silver normalization`. The stable evidence is the execution description plus the DAG `Filter` metric showing rows where `schema_version > 2`. In the current proof run, this filter outputs `6,774` rows, meaning the lakehouse contains breaking schema v3 events before the batch job normalizes or exports data to the Feast PostgreSQL offline store.

**Note for capture:** do not rely on the numeric SQL execution id because Spark regenerates ids after every rerun. Use browser search for `DP3 CHECK - count breaking schema_version rows before silver normalization`, then capture the full Spark UI page with the `Filter` node and `number of output rows` visible.

**Figure: Spark schema-evolution failure proof.** Capture the `kubectl logs` output from `spark-schema-evolution-fail-proof`. The important evidence is `ValueError: unsupported behavior_events schema_version=3`, followed by Spark aborting the task. This shows the failure mode explicitly: if the batch contract only supports v1/v2 and a breaking v3 event arrives, the Spark task fails instead of silently writing bad data.

**What to point out in the screenshot:** the generator has three schema phases: v1 old rows before `2026-03-23`, v2 evolved rows from `2026-03-23`, and v3 breaking rows from `2026-03-27`. The normal baseline Spark job counts v3 rows in the UI, while the fail-proof job intentionally treats v3 as unsupported to demonstrate the runtime schema-evolution problem.

**Analysis:** historical rows before the schema cutover may not have the same evolved fields as newer rows, and future rows may introduce a breaking contract. The normal baseline Spark job preserves old valid rows by normalizing missing fields, but the separate fail-proof job proves why schema contracts matter: an incompatible `schema_version=3` breaks the Spark task before offline-store export.

Code reference:

- [configs/local/data_generator_e2e_1k.yaml line 57](../../../configs/local/data_generator_e2e_1k.yaml#L57): generator schema evolution dates and breaking schema version.
- [apps/data-platform/data-generator/src/simulation.py line 236](../../../apps/data-platform/data-generator/src/simulation.py#L236): generator schema-version selection for v1/v2/v3 rows.
- [apps/data-platform/src/features/spark/build_silver_tables.py line 9](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L9): helper that adds missing columns only when needed.
- [infra/k8s/processing-baseline/spark-baseline-ui-job.yaml line 127](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L127): Spark UI action that counts breaking schema rows before silver normalization.
- [infra/k8s/processing-baseline/spark-schema-evolution-fail-job.yaml line 1](../../../infra/k8s/processing-baseline/spark-schema-evolution-fail-job.yaml#L1): Kubernetes job manifest for the intentional schema-evolution failure proof.

#### Duplicate Records, Events

**Standalone duplicate-detection proof script**

Use this when the reviewer wants a direct data proof in addition to the Spark UI `DP3 CHECK` row.

```bash
SPARK_POD=$(kubectl get pod -n recsys-dataflow -l app=spark-baseline-ui -o jsonpath='{.items[0].metadata.name}')
kubectl cp tools/proofs/detect_duplicate_events.py recsys-dataflow/${SPARK_POD}:/tmp/detect_duplicate_events.py
kubectl exec -n recsys-dataflow ${SPARK_POD} -- \
  /opt/spark/bin/spark-submit /tmp/detect_duplicate_events.py \
  --config /opt/recsys/configs/local/spark_batch_e2e_1k.yaml \
  --top 20
```

Capture the output line:

```text
DUPLICATE_EVENT_PROOF={...}
```

Also capture the `Top duplicated event_id values` table and the `Top conflicting duplicated event_id values` table. The key fields are `raw_behavior_event_rows`, `distinct_event_ids`, `duplicate_event_id_count`, `duplicate_extra_rows`, and `conflicting_duplicate_event_id_count`.

**Image proof: duplicate events detected directly from raw lakehouse behavior events**

![Duplicate events proof from Spark duplicate detection script](../../pngs/duplicate_events_proof.png)

**Figure: Duplicate-event proof from `docs/pngs/duplicate_events_proof.png`.** This screenshot shows the standalone Spark proof script reading the same raw lakehouse `behavior_events` input used by the batch job. The highlighted `DUPLICATE_EVENT_PROOF` summary reports `50,179` raw behavior-event rows, `30,751` distinct `event_id` values, `16,863` duplicated event ids, `19,428` extra duplicate rows, and `5,615` duplicated event ids with conflicting payload versions.

**What to point out in the screenshot:** the first table lists duplicated `event_id` values where `row_count=3`, proving the same business event id appears multiple times in raw data. The second table lists duplicated ids with `payload_versions=2`, proving some duplicates are conflicting duplicates rather than byte-for-byte repeats. This is stronger than only showing the Spark UI count because it exposes both the duplicate volume and concrete duplicate examples.

**Spark UI companion proof:** capture the Spark UI SQL execution labelled `DP3 CHECK - count rejected duplicate event_id rows before offline-store write`. That UI stage proves the batch job rejects duplicate rows before writing offline feature tables, while the terminal proof above proves the duplicate events exist in the raw lakehouse input.

**Analysis:** the generator injects both exact duplicates and conflicting duplicates. The Spark silver-table builder ranks rows by `event_id` and latest `ingestion_ts`, keeps the latest row, and places older duplicate rows in a rejected dataframe. The standalone script reads the same raw `behavior_events` table, groups by `event_id`, and reports duplicated ids plus conflicting duplicate payloads so the duplicate problem can be captured without opening the Spark DAG.

Code reference:

- [configs/local/data_generator_e2e_1k.yaml line 41](../../../configs/local/data_generator_e2e_1k.yaml#L41): duplicate and conflicting duplicate rates.
- [apps/data-platform/src/features/spark/build_silver_tables.py line 29](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L29): event-id dedup rule using latest `ingestion_ts`.
- [infra/k8s/processing-baseline/spark-baseline-ui-job.yaml line 153](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L153): Spark UI action that counts rejected duplicates.
- [tools/proofs/detect_duplicate_events.py line 1](../../../tools/proofs/detect_duplicate_events.py#L1): standalone duplicate-event proof script for screenshot capture.

### Develop Batch Processing Script To Handle Offline Problems

#### Skew Problems

**Technique used:** identify hot keys in Spark UI, then split the hottest key across salt buckets before partitioned aggregation. The comparison proof models the baseline partitioning with `category_id` only and the optimized partitioning with `(category_id, salt)`.

**Technique reference:** [Spark SQL Performance Tuning - Optimizing Skew Join](https://spark.apache.org/docs/latest/sql-performance-tuning.html#optimizing-skew-join). Spark documents skew handling through Adaptive Query Execution by splitting skewed shuffle partitions. In this repo's proof script, the same concept is applied explicitly by salting the hottest `category_id` before partitioned aggregation, so one hot category is spread across several partitions instead of overloading one partition.

Code reference:

- [tools/proofs/compare_spark_offline_optimizations.py line 73](../../../tools/proofs/compare_spark_offline_optimizations.py#L73): applies the salted hot-category partitioning used to reduce skew.

#### High Cardinality

**Technique used:** generate a large entity/id space through the data generator config, expose the baseline pressure with exact `distinct().count()`, then use Spark `approx_count_distinct(..., 0.05)` as the lightweight cardinality estimator proof. The batch job still converts raw behavior logs into compact user, item, and sequence feature tables before exporting to the Feast PostgreSQL offline store.

**Technique reference:** [PySpark approx_count_distinct](https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.approx_count_distinct.html). Spark provides `approx_count_distinct(col, rsd)` for approximate cardinality estimation with a configurable relative standard deviation. This is the right optimization when the pipeline needs a high-cardinality signal for monitoring or validation, but does not need to materialize every distinct `product_id` or `user_id`.

Code reference:

- [tools/proofs/compare_spark_offline_optimizations.py line 156](../../../tools/proofs/compare_spark_offline_optimizations.py#L156): compares exact distinct `product_id` with the optimized `approx_count_distinct(product_id, 0.05)` path.

#### Schema Evolution

**Technique used:** normalize evolved columns in the silver layer before feature computation, then gate unsupported breaking schema rows before they enter the feature path. Old v1/v2 rows are preserved with default values, while v3 rows are quarantined/fail-fast in the proof job.

**Technique reference:** [Spark Parquet Schema Merging](https://spark.apache.org/docs/latest/sql-data-sources-parquet.html#schema-merging). Spark supports compatible schema evolution by merging schemas, but the feature-store path uses a stricter contract: compatible v1/v2 rows are normalized, while unsupported v3 rows are quarantined before offline-store export.

Code reference:

- [tools/proofs/compare_spark_offline_optimizations.py line 170](../../../tools/proofs/compare_spark_offline_optimizations.py#L170): separates supported schema rows from unsupported v3 rows before the optimized feature path.

#### Duplicate Records, Events

**Technique used:** event-id deduplication ordered by ingestion time. The latest version of an event is kept, and older duplicate rows are rejected before offline-store export.

**Technique reference:** [PySpark dropDuplicates](https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.DataFrame.dropDuplicates.html). Spark provides built-in duplicate removal, but this repo uses a more explicit event-correctness rule for offline features: window by `event_id`, order by latest `ingestion_ts`, keep the latest row, and write older duplicate rows to the rejected dataset.

Code reference:

- [apps/data-platform/src/features/spark/build_silver_tables.py line 29](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L29): applies event-id deduplication with latest `ingestion_ts` ordering in the Spark silver-table job.

### View Spark UI To Show Problems Have Been Minimized

#### Optimized Comparison Proof Artifact

The optimized proof is produced from the same generated lakehouse `behavior_events` table as the baseline Spark UI proof. Keep this artifact inside the Spark UI proof section because it is the numeric companion for the UI screenshots: the UI shows *where* Spark spent work, while the JSON line shows *how much* each optimization reduced the problem.

**Rerun comparison command**

```bash
SPARK_POD=$(kubectl get pod -n recsys-dataflow -l app=spark-baseline-ui -o jsonpath='{.items[0].metadata.name}')
kubectl cp tools/proofs/compare_spark_offline_optimizations.py recsys-dataflow/${SPARK_POD}:/tmp/compare_spark_offline_optimizations.py
kubectl exec -n recsys-dataflow ${SPARK_POD} -- \
  /opt/spark/bin/spark-submit /tmp/compare_spark_offline_optimizations.py \
  --config /opt/recsys/configs/local/spark_batch_e2e_1k.yaml \
  --partitions 8 \
  --salt-buckets 8 \
  --output /tmp/spark_offline_optimization_comparison.json
kubectl cp recsys-dataflow/${SPARK_POD}:/tmp/spark_offline_optimization_comparison.json docs/submission/rubic-\(mini-coursework\)/spark_offline_optimization_comparison.json
```

![comparision run proof](../../pngs/spark_comparision_run.png)

**Figure: Spark offline optimization comparison run.** The screenshot captures the comparison script running inside the Spark proof pod. The highlighted `SPARK_OFFLINE_OPTIMIZATION_COMPARISON={...}` line is the compact proof to pair with the Spark UI captures: it reports baseline and optimized values for skew, high cardinality, schema evolution, and duplicate events from the same local lakehouse input.

Current comparison report:

- [docs/submission/rubic-(mini-coursework)/spark_offline_optimization_comparison.json line 1](spark_offline_optimization_comparison.json#L1): baseline vs optimized comparison output from the latest run.

**Result explanation from the artifact:** skew salting reduced max partition rows from `30,698` to `11,601`, so the hottest partition pressure dropped by `62.21%`. The partition skew ratio moved from `7.9862` to `3.018`, matching the more balanced Spark UI task distribution below. For high cardinality, exact `product_id` distinct count was `10,109`; the optimized `approx_count_distinct(product_id, 0.05)` estimate was `9,977`, only `1.31%` away from exact while avoiding a full exact distinct materialization for monitoring. Schema handling quarantined all `6,774` unsupported v3 rows before the feature path. Duplicate handling rejected `19,428` extra duplicate rows, leaving `0` duplicate extras after dedup.

#### Skew Problems

**Spark UI navigation**

1. Open `SQL / DataFrame`.
2. Open `DP3 OPTIMIZED - salted category_id partition load after skew handling`.
3. Click the associated job/stage.
4. Capture `Event Timeline` and `Summary Metrics`.
5. Compare with the baseline `DP3 CHECK - category_id partition load before skew salting` screenshot.

![Spark skew handled proof](../../pngs/data_skew_optimized.png)

**Figure: Spark skew minimized proof.** The optimized stage shows `4` completed tasks with very similar task durations: min `73 ms`, median `74 ms`, and max `75 ms`. The shuffle-read distribution is also tightly grouped: min `317.5 KiB / 7,550 records`, median `324.6 KiB / 7,739 records`, and max `327.1 KiB / 7,787 records`.

**Analysis:** this is the post-salting proof. Instead of one partition carrying most of the hot `category_id` work, the salted aggregation distributes the workload across partitions. The UI evidence is the narrow spread in both duration and shuffle-read records across tasks; the companion JSON confirms the skew ratio reduction from `7.9862` to `3.018`.

#### High Cardinality

**Spark UI navigation**

1. Open `SQL / DataFrame`.
2. Open the optimized cardinality query, currently shown as `collect at /tmp/compare_spark_offline_optimizations.py`.
3. Scroll below the DAG and expand **Physical Plan > Details**.
4. Capture the `HashAggregate` physical plan line that contains `partial_approx_count_distinct(product_id..., 0.05...)`.
5. Pair it with the comparison artifact above, which shows exact distinct vs approximate distinct error.

![Spark high cardinality handled proof](../../pngs/high_cardinality_optimized_sql.png)

**Figure: Spark high-cardinality minimized proof.** The screenshot is from Spark SQL physical plan details. It highlights `partial_approx_count_distinct(product_id#268L, 0.05, 0, 0)` inside `HashAggregate`, proving that the optimized path estimates product cardinality using Spark's approximate distinct-count aggregate instead of materializing the full exact distinct set.

**Analysis:** the baseline exact distinct check must shuffle and materialize unique product ids. The optimized proof replaces that with an approximate cardinality estimator for the monitoring/check path. The comparison artifact reports exact `10,109` vs approximate `9,977`, a `1.31%` relative error, which is accurate enough for a data-quality signal while reducing pressure from exact high-cardinality distinct computation.

## Flink Job To Handle Streaming Data Problems

The streaming path uses PostgreSQL CDC to Kafka topic `cdc.behavior_events`, then two continuous Flink jobs process events into feature stores:

- Offline-store Flink job writes processed streaming features to the Feast PostgreSQL offline feature store.
- Online-store Flink job writes low-latency online features to Redis.

Code reference:

- [infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml line 47](../../../infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml#L47): submits the online-store Flink job.
- [infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml line 135](../../../infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml#L135): submits the offline-store Flink job.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 993](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L993): assigns event-time watermarks to the Kafka CDC source.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 1014](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L1014): starts the keyed dedup/process stream.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 1018](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L1018): creates the streaming quality-window branch.

### View Flink UI To Show Baseline Problems

#### Bursty Traffic

**Flink UI navigation**

1. Open the Flink dashboard at `http://localhost:18083` and choose one of the continuous realtime jobs:
   `recsys-native-pyflink-realtime-features-online-recsys-flink-realtime-online`
   or `recsys-native-pyflink-realtime-features-online-recsys-flink-realtime-offline`.
2. Open the job **Overview** page and capture the operator graph. The graph should show `Source: cdc-behavior-events-source`, `streaming-quality-window-metrics`, `late-event-drop-policy`, and the online/offline writer path.
3. Click the `KEYED PROCESS -> (..., late-event-drop-policy, ...)` vertex.
4. Open **Metrics** and add `0._stream_key_by_map_operator.numRecordsOutPerSecond`, `0.late-event-drop-policy___stream_key_by_map_operator.numRecordsInPerSecond`, `0.busyTimeMsPerSecond`, `0.accumulateBackPressuredTimeMs`, and `0.mailboxLatencyMs_p95`.
5. Capture the metric graphs while the producer is running. In the current proof images, the useful signals are the rising records/second graph, `Busy (max): 99%`, the `accumulateBackPressuredTimeMs` step, and the `mailboxLatencyMs_p95` spike.
6. Optionally click `Source: cdc-behavior-events-source -> Map, Filter -> ...` and add `0.numRecordsOutPerSecond` if the reviewer wants a source-side view of the incoming Kafka burst.
7. Open the job-level **BackPressure** tab and capture the color/status for source, policy, metric, and writer operators.
8. Open the job-level **Checkpoints** tab and capture checkpoint duration/alignment data during a burst.

![Flink realtime job graph for streaming problem proof](../../pngs/flink_bursty_traffic_runtime.png.png)

**Figure: Flink realtime job graph for streaming problem proof.** This image shows the continuous CDC job running from `Source: cdc-behavior-events-source` into `late-event-drop-policy`, then splitting into the `streaming-quality-window-metrics` branch and the Feast PostgreSQL offline writer branch. The table below the graph shows the job is `RUNNING` and has already processed hundreds of thousands of records, so the proof is taken from the real Kafka CDC to feature-store path rather than a standalone demo job.

![Flink bursty traffic throughput proof](../../pngs/burst_ui_1.png)

**Figure: Flink bursty-traffic throughput proof.** This image focuses on the `KEYED PROCESS -> (..., late-event-drop-policy, ...)` vertex. The selected vertex is `RUNNING`, has `Busy (max): 99%`, and the metric graph for `numRecordsOutPerSecond` rises from roughly `420` to `550` records/second. That upward rate movement is the UI symptom of the generator's burst windows: records arrive faster than the operator can smoothly process them, so the operator stays almost fully busy.

![Flink bursty traffic pressure proof](../../pngs/burst_ui_2.png)

**Figure: Flink bursty-traffic pressure proof.** This image keeps the same `late-event-drop-policy` vertex selected and adds pressure metrics. `accumulateBackPressuredTimeMs` steps upward, while `mailboxLatencyMs_p95` jumps from about `2080 ms` to about `2280 ms` during the burst interval. The important reader takeaway is that burst traffic is visible not only as higher throughput, but also as increased operator scheduling/mailbox latency and accumulated backpressure time.

**Analysis:** every 5th realtime producer tick multiplies a normal `40` event tick into a `320` event burst. Flink UI does not label a spike as "burst" directly; it exposes the symptoms through source output-rate spikes, quality-metric input-rate spikes, backpressure, busy time, and checkpoint duration/alignment. The quality output also emits `is_bursty=true` when the window crosses the configured burst threshold.

Code reference:

- [infra/helm/recsys-data-platform/values.yaml line 133](../../../infra/helm/recsys-data-platform/values.yaml#L133): normal `40` events per tick.
- [infra/helm/recsys-data-platform/values.yaml line 146](../../../infra/helm/recsys-data-platform/values.yaml#L146): 8x burst every 5 ticks.
- [apps/data-platform/data-generator/src/scripts/run_realtime_postgres_producer.py line 333](../../../apps/data-platform/data-generator/src/scripts/run_realtime_postgres_producer.py#L333): burst multiplier implementation.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 875](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L875): streaming quality-window processor.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 894](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L894): computes the `is_bursty` flag.

#### Late Arrival Problems

**Flink UI navigation**

1. Open the same continuous Flink job used for the burst proof.
2. Click the `KEYED PROCESS -> (..., late-event-drop-policy, ...)` vertex.
3. Open **Metrics** and add `0.late-event-drop-policy___stream_key_by_map_operator.numRecordsIn`, `0.late-event-drop-policy___stream_key_by_map_operator.numRecordsOut`, `0.late-event-drop-policy___stream_key_by_map_operator.numRecordsInPerSecond`, and `0.late-event-drop-policy___stream_key_by_map_operator.numRecordsOutPerSecond`.
4. Add `0.late-event-drop-policy___stream_key_by_map_operator.currentInputWatermark` and `0.late-event-drop-policy___stream_key_by_map_operator.currentOutputWatermark` when you want to prove event-time progress from the Metrics tab.
5. Pair the UI screenshot with TaskManager log lines from `streaming-quality-window-metrics` if the reviewer wants the exact late counters. The log output contains `late_event_count`, `max_late_by_seconds`, `late_events_dropped`, and `side_output_late_events`.
6. Do not use the Flink **Watermarks** tab for this proof. In this local PyFlink job, the vertex-level Metrics tab is clearer because it can show both watermarks and the `numRecordsIn` versus `numRecordsOut` drop effect.

![Flink late arrival dropped-count proof](../../pngs/late_arrival_lag_1.png)

**Figure: Flink late-arrival drop-count proof.** This image selects the same production `late-event-drop-policy` vertex and compares the scoped metrics `late-event-drop-policy.numRecordsIn` and `late-event-drop-policy.numRecordsOut`. `numRecordsIn` climbs beyond `600,000`, while `numRecordsOut` stays at `0`, which means the policy is receiving CDC records but dropping them before the feature update path because the current stress run generated too-late events and `dropLateEvents=true`.

![Flink late arrival dropped-rate proof](../../pngs/late_arrival_lag_2.png)

**Figure: Flink late-arrival drop-rate proof.** This image shows the per-second version of the same policy check. `late-event-drop-policy.numRecordsInPerSecond` fluctuates around roughly `470-515` records/second, while `late-event-drop-policy.numRecordsOutPerSecond` remains `0`. This is the easiest Flink UI proof that late arrival is not just counted in logs; the operator is actively filtering the stream at runtime.

![Flink late arrival watermark metric proof](../../pngs/late_arrival_lag_3.png)

**Figure: Flink late-arrival watermark metric proof.** This image shows `currentInputWatermark` and `currentOutputWatermark` for the `late-event-drop-policy` vertex. Both watermark metrics move forward over time, proving the job is running with event-time/watermark awareness. Pair this with the previous `numRecordsIn` versus `numRecordsOut` screenshots: the watermark metrics show event-time progress, while the in/out metrics show the actual late-event drop effect.

**Analysis:** the realtime producer intentionally emits late events with event timestamps `45-180` minutes behind processing time. The Flink job computes late distance from event time versus processing time and the configured watermark delay. In the UI, late-arrival pressure is shown by the quality output receiving records; the output then quantifies how many records in each fixed event-time window are late and how many were dropped from the feature write path.

Code reference:

- [infra/helm/recsys-data-platform/values.yaml line 142](../../../infra/helm/recsys-data-platform/values.yaml#L142): late-arrival and out-of-order rates.
- [apps/data-platform/data-generator/src/scripts/run_realtime_postgres_producer.py line 350](../../../apps/data-platform/data-generator/src/scripts/run_realtime_postgres_producer.py#L350): emits late/out-of-order timestamps.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 246](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L246): computes late-arrival distance and late flag.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 721](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L721): applies late-event filtering before feature writes.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 875](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L875): records late/drop counters in quality windows.

### Develop Stream Processing Script To Handle Streaming Problems

#### Bursty Traffic

**Technique used:** event-time quality windows, burst thresholds, throughput monitoring, and backpressure/checkpoint monitoring.

**Best-practice reference:** [Flink Windows](https://nightlies.apache.org/flink/flink-docs-master/docs/dev/datastream/operators/windows/). Flink describes windows as the core mechanism for splitting an infinite stream into finite buckets for computation. The streaming job applies that pattern by assigning CDC events into fixed event-time quality windows and marking a window as bursty when `event_count >= burst_threshold_event_count`.

**From analysis above:** the realtime producer emits normal `40`-event ticks and periodic `320`-event burst ticks. The Flink UI proof should show this as source/output-rate spikes, records sent/received movement, and possible backpressure changes, while the job keeps the burst signal in the `streaming_quality_windows` output.

Code reference:

- [apps/data-platform/src/features/flink/realtime_stream_job.py line 875](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L875): runtime `StreamingQualityRows` keyed process function for finite quality windows.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 894](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L894): applies the burst threshold and emits `is_bursty`.
- [infra/helm/recsys-data-platform/values.yaml line 197](../../../infra/helm/recsys-data-platform/values.yaml#L197): runtime window/watermark/state settings.

#### Late Arrival

**Technique used:** event-time timestamp assignment, bounded-out-of-orderness watermarks, idle source detection, allowed lateness, late-event side output/DLQ, reconciliation/backfill path, and state TTL.

**Best-practice reference:** [Flink Generating Watermarks](https://nightlies.apache.org/flink/flink-docs-master/docs/dev/datastream/event-time/generating_watermarks/). Flink defines `WatermarkStrategy` as the combination of timestamp assignment and watermark generation, documents Python `for_bounded_out_of_orderness(...)`, recommends applying watermark strategy directly at the source when possible, documents `.with_idleness(...)` for idle source/partition handling, and documents `.with_watermark_alignment(...)` for keeping fast sources from moving too far ahead of slow ones.

**Best-practice reference:** [Flink Windows - Allowed Lateness and Side Output Late Data](https://nightlies.apache.org/flink/flink-docs-master/docs/dev/datastream/operators/windows/#allowed-lateness). Flink window APIs expose `.allowed_lateness(...)` and `.side_output_late_data(...)`; in this repo, the production feature path implements the same policy explicitly with `allowed_lateness_seconds`, `late-event-drop-policy`, and a `late-events-side-output` branch because the feature writers are keyed process/sink operators rather than pure Flink window operators.

**Best-practice reference:** [Flink Working with State - State Time-To-Live](https://nightlies.apache.org/flink/flink-docs-master/docs/dev/datastream/fault-tolerance/state/#state-time-to-live-ttl). Flink supports TTL on keyed state descriptors so dedup/history/window state does not grow forever. This repo enables TTL on dedup, user-history, item-history, and quality-window state.

**From analysis above:** the realtime producer emits late and out-of-order event timestamps. The Flink job now extracts event time from `event_timestamp`, uses a bounded watermark delay for out-of-order data, marks idle Kafka partitions, optionally supports watermark alignment, applies `allowed_lateness_seconds` to decide whether a late event can update features, writes too-late events to `stream_late_events_dlq`, and keeps `late_event_count`, `max_late_by_seconds`, `late_events_dropped`, and `side_output_late_events` in the quality-window output.

Code reference:

- [apps/data-platform/src/features/flink/realtime_stream_job.py line 993](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L993): bounded-out-of-orderness watermark strategy is assigned at the Kafka source.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 996](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L996): idle source detection via `with_idleness(...)`.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 246](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L246): `late_arrival_metrics` compares processing-time lag against `allowed_lateness_seconds`.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 721](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L721): `KeepFeatureEvents` applies the late-event drop policy before feature writes.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 728](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L728): `KeepLateEvents` creates the late-event side-output branch.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 733](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L733): `PostgresLateEventDlqWriter` persists too-late events to `stream_late_events_dlq` for reconciliation/backfill.
- [infra/helm/recsys-data-platform/values.yaml line 197](../../../infra/helm/recsys-data-platform/values.yaml#L197): Helm config for watermark delay, allowed lateness, idleness, alignment, DLQ, and state TTL.


### View Flink UI To Show Problems Have Been Minimized

#### Bursty Traffic

![Flink optimized streaming metrics](../../pngs/flink_optimized.png)

> **Comparison with the baseline burst graphs above:** at the captured snapshot,
> `accumulateBackPressuredTimeMs` is about `6.12k ms`, compared with about
> `10.30k ms` in the baseline (`~4.18k ms`, or `~41%`, lower). The observed
> `mailboxLatencyMs_p95` peak is about `2.10 s`, down from the baseline peak of
> about `2.28 s` (`~180 ms`, or `~8%`, lower). Operator utilization remains high
> (`Busy (max): 100%` versus `99%`), so the improvement is not lower workload;
> it is that the same burst is handled with lower accumulated pressure and a
> lower mailbox-latency peak while the job remains `RUNNING`. Because
> `accumulateBackPressuredTimeMs` is cumulative and the screenshots come from
> different runtime windows, the `41%` value is snapshot evidence rather than a
> controlled benchmark. The stronger runtime result is confirmed by the next
> image: Back Pressure is `OK` and the subtask reports `0%` backpressure.

**Figure: optimized Flink operator metrics under bursty traffic.** The selected
`KEYED PROCESS -> (_stream_key_by_map_operator, late-events-side-output,
late-event-drop-policy, _stream_key_by_map_operator)` vertex remains `RUNNING`
while the Metrics tab tracks `accumulateBackPressuredTimeMs` and
`mailboxLatencyMs_p95`. This proves the burst is no longer turning into job
failure or restart; pressure still exists because the generator is pushing a
heavy stream, but it is bounded inside a running operator.

![Flink backpressure OK](../../pngs/backpressure_ok.png)

**Figure: BackPressure tab after handling bursty traffic.** The same keyed
operator reports `Back Pressure Status: OK`, with subtask backpressure at `0%`
and the task still `RUNNING`. The operator is busy processing the burst, but it
is not blocked by downstream backpressure.

![Flink UI operator names](../../pngs/flink_ui_names.png)

**Figure: operator-level proof that the optimized stream graph is active.** The
job graph shows the source, `late-events-side-output`, `late-event-drop-policy`,
`streaming-quality-window-metrics`, and online feature writer operators all in
`RUNNING` state. This confirms the proof is captured from the real continuous
Flink jobs, not from an isolated test operator.


### Window Processing

![Flink window processing proof](../../pngs/window_processing.png)

**Figure: Flink window processing proof.** Capture the Flink UI operator graph and/or the `streaming_quality_windows` output table with `window_start`, `window_end`, `event_count`, `late_event_count`, `late_events_dropped`, `side_output_late_events`, `duplicate_event_count`, `max_late_by_seconds`, and `is_bursty`. This proof connects Flink UI runtime behavior to finite event-time quality windows: burst traffic becomes `is_bursty`, late arrival becomes late/drop counters, and duplicates become `duplicate_event_count`.

**How the window code works:** `StreamingQualityRows` is a keyed process function that receives the deduplicated CDC event stream after the late-event policy branch. For each event, it reads `event_timestamp`, converts it to Unix seconds, then floors that timestamp into a fixed event-time bucket using `quality_window_seconds`. That creates deterministic `window_start` and `window_end` values, for example one row per 60-second event-time window.

Inside each window, the job keeps a small keyed state object named `stream_quality_window`. Every incoming event increments `event_count`. If `late_arrival_metrics(...)` marks the event as late, the same state increments `late_event_count`; if `drop_late_events` is enabled, it also increments `late_events_dropped`. Late events are also counted as `side_output_late_events` because the job has a separate late-event side-output/DLQ branch. Duplicate records are counted through the `_is_duplicate` marker produced by the upstream dedup operator, and `max_late_by_seconds` stores the worst lateness observed inside the window.

When the event belongs to a new window, the function emits the previous window row first, then starts a fresh state object for the new window. It also emits the current window state after each event so the proof table/log updates continuously while the stream is running. The `is_bursty` flag is computed from the current window volume: `event_count >= burst_threshold_event_count`.

**What this proves:** this is the actual stream-processing logic that turns raw CDC events into finite quality windows. It does not just print metrics; it groups the infinite Kafka stream by event time, tracks late/drop/duplicate/burst counters with Flink keyed state, and emits rows that can be captured from Flink UI/logs or the `streaming_quality_windows` sink.

Code reference:

- [apps/data-platform/src/features/flink/realtime_stream_job.py line 875](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L875): production `StreamingQualityRows` process function.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 899](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L899): computes late-arrival metrics per quality-window event.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 902](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L902): floors event time into a fixed quality window.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 922](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L922): updates window counters for event, late, dropped, side-output, duplicate, and max lateness.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 1130](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L1130): defines the `streaming_quality_windows` sink schema.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 1192](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L1192): enables checkpointing.

## Production Integration Proof

### Spark Batch Job Integrated Into Airflow Pipeline

Spark batch processing is integrated into two Airflow DAGs rather than being run as a standalone script. Airflow starts each workload with `spark-submit` in Kubernetes cluster mode, waits for the Spark driver application to finish, and only then allows the validation stage to run.

#### DP2: Spark Bronze To Silver/Gold Processing

In DAG `recsys_dp2_bronze_to_silver_gold`, both Airflow stages submit Spark applications. The `ingest_stage` reads the Bronze lakehouse data produced by DP1, normalizes timestamps and compatible schema changes, rejects duplicate or invalid behavior events, builds order facts and product SCD data, and writes the curated datasets as `silver_*` lakehouse tables. The following `validate_stage` reads every expected curated table with Spark and fails the DAG when any table is empty.

![DP2 Airflow DAG proof](../../pngs/dp2_airflow_ui.png)

**Figure: DP2 Spark integration in Airflow.** The Airflow Graph view shows the ordered `ingest_stage -> validate_stage` workflow in DAG `recsys_dp2_bronze_to_silver_gold`. Both green nodes prove that Spark completed the Bronze-to-Silver/Gold transformation and subsequently verified the resulting curated lakehouse tables.

#### DP3: Spark Offline Feature Engineering

In DAG `recsys_dp3_offline_feature_table`, the `ingest_stage` submits the production Spark batch feature job. Spark builds the clean input frames, computes `user_sequence_features`, `user_aggregate_features`, `item_features`, ranking labels, and the BST training dataset, writes the feature outputs to the feature lakehouse namespace, and exports the Feast-facing tables to PostgreSQL. PostgreSQL is the configured Feast offline store; Apache Iceberg remains the upstream lakehouse and feature-storage layer.

The DP3 `validate_stage` does not perform feature engineering. It connects to PostgreSQL after Spark finishes and runs row-count checks against every expected offline-store table. Therefore, the count checks are completion validation only; the actual transformations and feature calculations happen in the preceding Spark `ingest_stage`.

![DP3 Airflow DAG proof](../../pngs/dp3_airflow_ui.png)

**Figure: DP3 Spark integration in Airflow.** The Airflow Graph view shows `ingest_stage -> validate_stage` in DAG `recsys_dp3_offline_feature_table`. The successful Spark ingest node proves that feature computation and PostgreSQL export completed, while the successful validation node proves that the resulting Feast offline-store tables contain data.

Code reference:

- [apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py line 96](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L96): shared Spark-on-Kubernetes submission command used by the Airflow tasks.
- [apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py line 209](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L209): DP2 Spark ingest and validation commands.
- [apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py line 245](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L245): DP2 Airflow DAG and ordered stages.
- [apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py line 17](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py#L17): DP2 Bronze-to-Silver/Gold Spark processing.
- [apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py line 140](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L140): DP3 Spark batch command.
- [apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py line 266](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L266): DP3 Airflow DAG and ordered stages.
- [apps/data-platform/src/features/spark/spark_batch_entrypoint.py line 111](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L111): production Spark batch entrypoint.
- [apps/data-platform/src/features/spark/spark_batch_entrypoint.py line 43](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L43): computes the DP3 feature and training outputs.
- [apps/data-platform/src/features/spark/spark_batch_entrypoint.py line 98](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L98): exports Feast-facing feature tables into PostgreSQL.
