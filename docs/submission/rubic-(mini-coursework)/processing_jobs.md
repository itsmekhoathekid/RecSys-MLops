# Processing Jobs

This page documents the current runtime proof plan for Spark batch processing and Flink stream processing. The proof is based on the real data generator, lakehouse input, Kafka CDC stream, Spark UI, Flink UI, and feature-store outputs.

## Current Data Generator Data Problems Config

### Batch generator for lakehouse data

The batch generator writes raw recommendation-system data into the lakehouse with data issues turned on, so the Spark batch job can process realistic offline data problems before exporting features to the Feast PostgreSQL offline store.

Code reference:

- [data_generator_e2e_1k.yaml (line 7)](../../../configs/local/data_generator_e2e_1k.yaml#L7), [data_generator_e2e_1k.yaml (line 48)](../../../configs/local/data_generator_e2e_1k.yaml#L48): batch traffic and high-cardinality volume.
- [data_generator_e2e_1k.yaml (line 33)](../../../configs/local/data_generator_e2e_1k.yaml#L33), [data_generator_e2e_1k.yaml (line 47)](../../../configs/local/data_generator_e2e_1k.yaml#L47): skewed city/category distribution knobs.
- [data_generator_e2e_1k.yaml (line 58)](../../../configs/local/data_generator_e2e_1k.yaml#L58): exact-duplicate configuration.
- [data_generator_e2e_1k.yaml (line 54)](../../../configs/local/data_generator_e2e_1k.yaml#L54), [data_generator_e2e_1k.yaml (line 57)](../../../configs/local/data_generator_e2e_1k.yaml#L57): schema evolution and breaking-schema config.

The current batch config is intentionally stress-heavy. It uses a large entity space (`20,000` products, `8,000` users, `5,000` brands, `1,000` categories) for high-cardinality proof. Category and city distributions are uneven (`top_category_ratio=0.99`, `top_city_ratio=0.96`) for skew proof. Exact duplicates use `duplicate_event_rate=0.45`, and schema evolution has a compatible cutover on `2026-03-23` plus breaking v3 rows after `2026-03-27`. These are the four offline problem groups: skew, high cardinality, schema evolution, and duplicates.

### Streaming generator for Kafka CDC and Flink jobs

The realtime producer continuously inserts source rows into PostgreSQL. CDC then sends behavior events to Kafka topic `cdc.behavior_events`, where the two continuous Flink jobs consume them:

- Flink offline-store job writes processed streaming features to the Feast PostgreSQL offline store.
- Flink online-store job writes online features to Redis.

Code reference:

- [data_generator_e2e_1k.yaml (line 61)](../../../configs/local/data_generator_e2e_1k.yaml#L61), [data_generator_e2e_1k.yaml (line 78)](../../../configs/local/data_generator_e2e_1k.yaml#L78): streaming generator plus burst, late-arrival, and duplicate settings in the shared scenario config.
- [problem_pipeline.py (line 23)](../../../apps/data-platform/data-generator/src/streaming/problem_pipeline.py#L23), [producer.py (line 20)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L20), [producer.py (line 35)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L35): three-class problem wiring, producer entrypoint, and continuous emission loop.

The streaming config contains exactly three problems. A normal tick emits `40` events and every fifth tick multiplies it by `8`; recent events are replayed at `14%`; and late events are backdated by `45–180` minutes at `28%`.

## Spark Job To Handle Offline Data Problems

The Spark batch job reads raw tables from the data lakehouse, normalizes/deduplicates them into silver tables, computes offline feature tables, writes Iceberg feature tables, and exports to the Feast PostgreSQL offline feature store.

Code reference:

- [spark_batch_entrypoint.py (line 34)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L34), [spark_batch_entrypoint.py (line 39)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L39), [spark_batch_entrypoint.py (line 152)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L152), [spark_batch_entrypoint.py (line 209)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L209): configuration loading, source resolution, production batch flow, and CLI entrypoint.
- [spark_batch_entrypoint.py (line 53)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L53), [spark_batch_entrypoint.py (line 76)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L76), [spark_batch_entrypoint.py (line 180)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L180), [spark_batch_entrypoint.py (line 186)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L186): selects or builds Silver inputs and constructs the offline feature outputs.
- [spark_batch_entrypoint.py (line 93)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L93), [spark_batch_entrypoint.py (line 102)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L102), [spark_batch_entrypoint.py (line 108)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L108), [spark_batch_entrypoint.py (line 112)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L112): configures the PostgreSQL export, prepares the Feast tables, and writes each batch feature row.

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

[spark-baseline-ui-job.yaml (line 67)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L67), [spark-baseline-ui-job.yaml (line 74)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L74), [spark-baseline-ui-job.yaml (line 161)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L161), [spark-baseline-ui-job.yaml (line 189)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L189), [spark-baseline-ui-job.yaml (line 212)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L212) defines the baseline Spark settings, both heavy SQL queries, and their UI actions. The core skew line is the `CASE WHEN category_id = 1 THEN 24 ELSE 2 END` multiplier: rows from the hot category are expanded 24 times, while other categories are expanded only 2 times. This keeps the proof deterministic and makes the skew obvious in one SQL execution with 32 shuffle tasks.

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

- [data_generator_e2e_1k.yaml (line 33)](../../../configs/local/data_generator_e2e_1k.yaml#L33), [data_generator_e2e_1k.yaml (line 47)](../../../configs/local/data_generator_e2e_1k.yaml#L47): skewed category and city distribution config.
- [spark-baseline-ui-job.yaml (line 161)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L161), [spark-baseline-ui-job.yaml (line 173)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L173), [spark-baseline-ui-job.yaml (line 212)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L212): heavy skew SQL, hot-category amplification, and the Spark UI action that executes it.

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

[spark-baseline-ui-job.yaml (line 189)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L189), [spark-baseline-ui-job.yaml (line 196)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L196), [spark-baseline-ui-job.yaml (line 203)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L203), [spark-baseline-ui-job.yaml (line 204)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L204), [spark-baseline-ui-job.yaml (line 216)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L216) defines the heavy high-cardinality SQL, composite key, exact/approximate measures, and UI action. The important field is `product_event_key`, which combines `product_id`, `event_id`, and `repeat_id` so Spark has to aggregate many near-unique keys.

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

- [data_generator_e2e_1k.yaml (line 48)](../../../configs/local/data_generator_e2e_1k.yaml#L48), [data_generator_e2e_1k.yaml (line 53)](../../../configs/local/data_generator_e2e_1k.yaml#L53): high-cardinality entity counts and preferences per user.
- [spark-baseline-ui-job.yaml (line 196)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L196), [spark-baseline-ui-job.yaml (line 203)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L203), [spark-baseline-ui-job.yaml (line 204)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L204): composite key, exact distinct count, and `approx_count_distinct(..., 0.05)`.

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

- [data_generator_e2e_1k.yaml (line 54)](../../../configs/local/data_generator_e2e_1k.yaml#L54), [data_generator_e2e_1k.yaml (line 57)](../../../configs/local/data_generator_e2e_1k.yaml#L57): schema evolution dates and breaking version.
- [simulation.py (line 234)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L234), [simulation.py (line 236)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L236), [simulation.py (line 238)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L238), [simulation.py (line 240)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L240), [simulation.py (line 288)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L288), [simulation.py (line 290)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L290), [simulation.py (line 292)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L292): v3/v1/v2 selection and version-dependent request-field population.
- [build_silver_tables.py (line 17)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L17), [build_silver_tables.py (line 28)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L28), [build_silver_tables.py (line 30)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L30), [build_silver_tables.py (line 41)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L41), [build_silver_tables.py (line 45)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L45): compatible-column normalization, schema-version gating, and event-ID deduplication.
- [spark-baseline-ui-job.yaml (line 55)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L55), [spark-baseline-ui-job.yaml (line 125)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L125), [spark-baseline-ui-job.yaml (line 130)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L130): fail helper, breaking-row count, and optional fail-proof action.
- [spark-schema-evolution-fail-job.yaml (line 26)](../../../infra/k8s/processing-baseline/spark-schema-evolution-fail-job.yaml#L26), [spark-schema-evolution-fail-job.yaml (line 47)](../../../infra/k8s/processing-baseline/spark-schema-evolution-fail-job.yaml#L47): Spark submission and `--fail-on-breaking-schema` flag in the intentional failure manifest.

#### Duplicate Records, Events

Use the checked-in generator summary for source-side duplicate counts and the Spark UI job for the post-deduplication check:

```bash
PYTHONPATH=apps/data-platform/data-generator/src uv run python \
  apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py \
  --config configs/local/data_generator_e2e_1k.yaml \
  --lake-root data_platform/lake | \
  awk '/## Duplicate Rate Before And After Dedup/{flag=1} /^## Injected Vs Observed/{flag=0} flag'
```

**Image proof: duplicate events detected in generated data**

![Duplicate events proof from Spark duplicate detection script](../../pngs/duplicate_events_proof.png)

**Figure: Duplicate-event proof from `docs/pngs/duplicate_events_proof.png`.** The capture records raw row count, distinct event IDs, and exact duplicate rows from the generated input used by the Spark proof.

**Spark UI companion proof:** capture the Spark UI action labelled `DP3 CHECK - count supported rows removed by dropDuplicates(event_id)`. It subtracts the clean behavior-event count from the supported raw-event count, so it reports the rows removed by `.dropDuplicates(["event_id"])` without mixing in unsupported-schema rows.

**Analysis:** the generator injects exact duplicates. The Silver builder normalizes supported rows and calls `.dropDuplicates(["event_id"])` before offline-store export. Spark keeps one arbitrary row for each duplicate event ID; this implementation does not promise that the surviving row has the latest `ingestion_ts`. `silver_rejected_behavior_events` now contains unsupported-schema rows only, not the rows removed by deduplication.

Code reference:

- [data_generator_e2e_1k.yaml (line 58)](../../../configs/local/data_generator_e2e_1k.yaml#L58), [data_generator_e2e_1k.yaml (line 59)](../../../configs/local/data_generator_e2e_1k.yaml#L59): exact-duplicate rate.
- [exact_duplicate.py (line 13)](../../../apps/data-platform/data-generator/src/offline/problems/exact_duplicate.py#L13), [exact_duplicate.py (line 14)](../../../apps/data-platform/data-generator/src/offline/problems/exact_duplicate.py#L14): selects exact duplicate events using the configured rate.
- [problem_pipeline.py (line 43)](../../../apps/data-platform/data-generator/src/offline/problem_pipeline.py#L43), [problem_pipeline.py (line 44)](../../../apps/data-platform/data-generator/src/offline/problem_pipeline.py#L44): injects the selected rows into the offline output.
- [summarize_generation_quality.py (line 119)](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L119), [summarize_generation_quality.py (line 122)](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L122), [summarize_generation_quality.py (line 123)](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L123), [summarize_generation_quality.py (line 126)](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L126): raw-row, repeated-event-ID, and exact `(event_id, payload_hash)` duplicate calculations.
- [build_silver_tables.py (line 41)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L41), [build_silver_tables.py (line 44)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L44), [build_silver_tables.py (line 45)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L45), [build_silver_tables.py (line 46)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L46): quarantines unsupported schemas, gates supported rows, applies `.dropDuplicates(["event_id"])`, and returns the clean/rejected outputs.
- [spark-baseline-ui-job.yaml (line 150)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L150), [spark-baseline-ui-job.yaml (line 152)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L152), [spark-baseline-ui-job.yaml (line 153)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L153): Spark UI action and input-minus-clean count for rows removed by event-ID deduplication.

### Develop Batch Processing Script To Handle Offline Problems

#### Skew Problems

**Technique used:** expose hot-category pressure with the checked-in Spark UI proof query, then run production DP2/DP3 with Spark Adaptive Query Execution (AQE), partition coalescing, and an advisory partition size. The current implementation does not claim a custom salting algorithm.

**Technique reference:** [Spark SQL Performance Tuning — Adaptive Query Execution](https://spark.apache.org/docs/latest/sql-performance-tuning.html#adaptive-query-execution).

Code reference:

- [spark-baseline-ui-job.yaml (line 67)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L67), [spark-baseline-ui-job.yaml (line 74)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L74), [spark-baseline-ui-job.yaml (line 161)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L161), [spark-baseline-ui-job.yaml (line 173)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L173), [spark-baseline-ui-job.yaml (line 212)](../../../infra/k8s/processing-baseline/spark-baseline-ui-job.yaml#L212): reproducible skew-amplification query with AQE disabled for baseline visibility.
- [session.py (line 17)](../../../apps/data-platform/src/features/spark/session.py#L17), [session.py (line 18)](../../../apps/data-platform/src/features/spark/session.py#L18), [session.py (line 19)](../../../apps/data-platform/src/features/spark/session.py#L19), [session.py (line 22)](../../../apps/data-platform/src/features/spark/session.py#L22): production AQE, partition coalescing, and advisory partition sizing.

#### High Cardinality

**Technique used:** the production DP3 user-aggregate job computes seven-day category cardinality with `approx_count_distinct(category_id, 0.05)` inside the per-user event-time window. This replaces `collect_list` plus `array_distinct`, so the aggregate uses bounded HyperLogLog++ sketch state instead of materializing every category ID in each rolling window. The output contract remains `distinct_categories_7d`, with approximate semantics and a maximum requested relative standard deviation of `0.05`. In DP2, supported events are returned immediately after native event-ID deduplication; the previous global `orderBy(event_timestamp, event_id)` was removed because downstream user/item windows define their own key-local ordering and a global sort would add a full shuffle over high-cardinality rows.

**Technique reference:** [PySpark approx_count_distinct](https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.approx_count_distinct.html). Spark provides `approx_count_distinct(col, rsd)` for approximate cardinality estimation with configurable relative standard deviation. Here it is part of the production feature transformation rather than only a separate proof query.

Code reference:

- [build_user_aggregate_features.py (line 6)](../../../apps/data-platform/src/features/spark/build_user_aggregate_features.py#L6), [build_user_aggregate_features.py (line 36)](../../../apps/data-platform/src/features/spark/build_user_aggregate_features.py#L36): defines the `0.05` RSD contract and applies the approximate distinct-category aggregate inside the seven-day per-user window.
- [build_silver_tables.py (line 45)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L45), [build_silver_tables.py (line 46)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L46): deduplicates supported events and returns them without a global post-deduplication sort.

#### Schema Evolution

**Technique used:** this is now a real Parquet schema-merge path, not only rows with nullable columns. Historical V1 `behavior_events` files physically omit `device_type` and `campaign_id`; V2 files contain them. DP1 copies each Parquet fragment independently so those physical schemas remain different in Bronze. Spark enables `spark.sql.parquet.mergeSchema=true` both at session level and on the Parquet read. The Silver contract then fills compatible missing/null V1 fields, admits V1/V2, quarantines V3, and deduplicates only the supported rows.

**Technique reference:** [Spark Parquet Schema Merging](https://spark.apache.org/docs/latest/sql-data-sources-parquet.html#schema-merging). Spark supports compatible schema evolution by merging schemas, but the feature-store path uses a stricter contract: compatible v1/v2 rows are normalized, while unsupported v3 rows are quarantined before offline-store export.

Code reference:

- [sink.py (line 58)](../../../apps/data-platform/data-generator/src/sink.py#L58): removes the V2-only fields from the physical V1 Arrow schema.
- [sink.py (line 97)](../../../apps/data-platform/data-generator/src/sink.py#L97): chooses the physical schema separately for each date partition.
- [sink.py (line 110)](../../../apps/data-platform/data-generator/src/sink.py#L110): writes that physical schema into each Parquet file.
- [batch_lakehouse_ingestion.py (line 109)](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L109): reads each physical Parquet fragment without dataset-level schema merging.
- [batch_lakehouse_ingestion.py (line 125)](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L125): gives every fragment a distinct output file.
- [batch_lakehouse_ingestion.py (line 126)](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L126): persists each fragment independently, preserving its schema.
- [session.py (line 20)](../../../apps/data-platform/src/features/spark/session.py#L20): enables Parquet schema merging for the production Spark session.
- [session.py (line 55)](../../../apps/data-platform/src/features/spark/session.py#L55): explicitly enables `mergeSchema` on each Parquet table read.
- [build_silver_tables.py (line 28)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L28): supplies the V1 default for missing/null `device_type`.
- [build_silver_tables.py (line 29)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L29): supplies the V1 default for missing/null `campaign_id`.
- [build_silver_tables.py (line 41)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L41): selects unsupported V3+ rows.
- [build_silver_tables.py (line 42)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L42): labels those rows `unsupported_schema_version`.
- [build_silver_tables.py (line 44)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L44): gates the feature path to supported V1/V2 rows.
- [build_silver_tables.py (line 46)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L46): returns unsupported-schema rows as the rejected Silver output.

#### Duplicate Records, Events

**Technique used:** native Spark event-ID deduplication with `.dropDuplicates(["event_id"])`. One supported row per event ID is retained before offline-store export; duplicate copies are discarded rather than written to the rejected table.

**Technique reference:** [PySpark `DataFrame.dropDuplicates`](https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.DataFrame.dropDuplicates.html). Both Spark cleaning paths use the native operation: behavior events deduplicate on `event_id`, while impressions deduplicate on `impression_id`. Because `dropDuplicates` does not define which duplicate survives, the code does not claim latest-`ingestion_ts` selection.

**Measurement note:** `dropDuplicates` does not emit a removed-row counter by itself. The capture query measures `supported_rows_before_dedup - clean_rows_after_dedup`; its reduction percentage is `removed_rows / supported_rows_before_dedup * 100`. Unsupported V3 rows must be excluded from the input count because they follow the quarantine path rather than the deduplication path.

Code reference:

- [build_silver_tables.py (line 25)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L25), [build_silver_tables.py (line 45)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L45), [build_silver_tables.py (line 46)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L46): `build_clean_behavior_events` applies `.dropDuplicates(["event_id"])`, returns supported/unsupported paths separately, and does not globally sort the clean output.
- [build_silver_tables.py (line 49)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L49), [build_silver_tables.py (line 54)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L54), [build_silver_tables.py (line 55)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L55): `build_clean_impressions` converts the timestamp, applies `.dropDuplicates(["impression_id"])`, and orders the cleaned rows.

### View Spark UI To Show Problems Have Been Minimized

#### Reproducible Baseline/Production Comparison

The current reproducible comparison uses the checked-in baseline Kubernetes job and the production Spark session. The captured comparison artifact and UI screenshots below remain the numeric and visual proof from the earlier optimization run.

```bash
kubectl apply -f infra/k8s/processing-baseline/spark-baseline-ui-job.yaml
kubectl -n recsys-dataflow wait --for=condition=complete job/spark-baseline-ui --timeout=20m
kubectl -n recsys-dataflow logs job/spark-baseline-ui | \
  grep -E 'SPARK_LAKEHOUSE_TO_OFFLINE_STORE_BASELINE|DP3 (HEAVY SQL|CHECK)'
```

![comparision run proof](../../pngs/spark_comparision_run.png)

**Figure: Spark offline optimization comparison run.** The screenshot captures the comparison script running inside the Spark proof pod. The highlighted `SPARK_OFFLINE_OPTIMIZATION_COMPARISON={...}` line is the compact proof to pair with the Spark UI captures: it reports baseline and optimized values for skew, high cardinality, schema evolution, and duplicate events from the same local lakehouse input. For duplication, the captured run starts with `50,179` raw behavior-event rows, identifies `19,428` extra duplicate rows across `16,863` repeated event IDs (including `5,615` IDs with conflicting payloads), and produces `30,751` clean rows with `0` duplicate extras remaining, reported as a `100%` duplicate-extra-row reduction. The technique label embedded in this historical artifact refers to the earlier ordered-window implementation; the current production cleaner uses native Spark `.dropDuplicates(["event_id"])` and therefore guarantees one supported row per event ID but does not guarantee that the retained row has the latest `ingestion_ts`.

Current comparison report:

- [spark_offline_optimization_comparison.json (line 1)](spark_offline_optimization_comparison.json#L1): baseline vs optimized comparison output from the latest run.

**Result explanation from the artifact:** skew salting reduced max partition rows from `30,698` to `11,601`, so the hottest partition pressure dropped by `62.21%`. The partition skew ratio moved from `7.9862` to `3.018`, matching the more balanced Spark UI task distribution below. For high cardinality, exact `product_id` distinct count was `10,109`; the optimized `approx_count_distinct(product_id, 0.05)` estimate was `9,977`, only `1.31%` away from exact while avoiding a full exact distinct materialization for monitoring. Schema handling quarantined all `6,774` unsupported v3 rows before the feature path. Duplicate handling removed `19,428` extra duplicate rows, leaving `0` duplicate extras after dedup.

#### Skew Problems

**Spark UI navigation**

1. Open `SQL / DataFrame`.
2. Open `DP3 OPTIMIZED - salted category_id partition load after skew handling`.
3. Click the associated job/stage.
4. Capture `Event Timeline` and `Summary Metrics`.
5. Compare with the baseline `DP3 CHECK - category_id partition load before skew salting` screenshot.

![Spark skew handled proof](../../pngs/data_skew_optimized.png)

**Figure: Spark skew minimized proof.** The optimized stage shows `4` completed tasks with very similar task durations: min `73 ms`, median `74 ms`, and max `75 ms`. The shuffle-read distribution is also tightly grouped: min `317.5 KiB / 7,550 records`, median `324.6 KiB / 7,739 records`, and max `327.1 KiB / 7,787 records`.

**Analysis:** this captured controlled comparison is the post-salting proof. Instead of one partition carrying most of the hot `category_id` work, the salted aggregation distributes the workload across partitions. The UI evidence is the narrow spread in both duration and shuffle-read records across tasks; the companion JSON confirms the skew ratio reduction from `7.9862` to `3.018`. This is retained as optimization evidence; the current production implementation uses AQE/coalescing and does not claim a custom salting algorithm.

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

The deployed proof environment keeps a fixed runtime layout for both baseline and optimized observations: two TaskManagers with one slot each. The online job occupies one route and ends at the Redis sink; the offline job occupies the other route and ends at the PostgreSQL sink. This is the default deployment topology, not an optimization variable, so the UI comparisons below do not attribute metric changes to the number of TaskManagers.

Code reference:

- [realtime-flink-consumer.yaml (line 52)](../../../infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml#L52): gives the Redis job its own Kafka consumer group.
- [realtime-flink-consumer.yaml (line 144)](../../../infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml#L144): gives the PostgreSQL job a different Kafka consumer group.
- [realtime_stream_job.py (line 420)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L420): builds the native `KafkaSource`.
- [realtime_stream_job.py (line 875)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L875): connects that source to the production event-time graph.
- [realtime_stream_job.py (line 941)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L941): names the Redis online-store writer.
- [realtime_stream_job.py (line 951)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L951): names the PostgreSQL offline-store writer.

### View Flink UI To Show Baseline Problems

These screenshots are the **before-optimization baseline**, captured on 18 July 2026 from the GCP deployment built from branch `feats/unoptimized-processing-metrics` (commit `6f25ad7`). The online job uses Kafka consumer group `recsys-flink-baseline-online-store`; the equivalent offline job uses `recsys-flink-baseline-offline-store`. Both baseline jobs retain the same three late-event counters as the optimized build so that the two runs can be compared with the same definitions.

The baseline graph can be distinguished from the optimized graph by its manual `KEYED PROCESS, streaming-quality-window-metrics` operator. A clean-looking graph is not evidence of optimization: the runtime overlays and tabs below show that this graph reaches high backpressure and accumulates too-late events under the stress workload.

#### Bursty Traffic

The stress producer emits `40` events on a normal one-second tick and multiplies every fifth tick by eight, producing a `320`-event burst. The following captures show how the unoptimized job reacts.

![Unoptimized Flink baseline job overview under burst traffic](../../pngs/flink_baseline_job_overview.png)

**Figure: Unoptimized Flink baseline overview under burst traffic.** The real CDC-to-Redis online job is `RUNNING`, but the graph overlay records severe pressure: the source reaches `Backpressured (max): 99%`, the `watermark-lateness-classifier` reaches `80%`, and the feature branch remains busy up to `77%`. The table also shows records continuing to move through every stage. This is evidence of a live but saturated baseline, not a stopped or failed job.

![Unoptimized Flink baseline with HIGH backpressure](../../pngs/flink_baseline_high_backpressure.png)

**Figure: Unoptimized Flink baseline reports `HIGH` backpressure.** On the selected `watermark-lateness-classifier` subtask, Flink reports `66%` backpressured, `20%` idle, and `14%` busy, with the overall status marked `HIGH`. The job is still `RUNNING`; therefore the red status is direct runtime evidence that the burst workload is propagating downstream pressure through the manual baseline graph.

![Unoptimized Flink baseline burst throughput rates](../../pngs/flink_baseline_burst_throughput_rates.png)

**Figure: Unoptimized Flink baseline input and output rates during burst traffic.** The `watermark-lateness-classifier` input and output rates move together through repeated plateaus of approximately `60-69 records/s`, then drop to about `54 records/s` near the end of the capture. Flink displays rolling rates rather than the producer's instantaneous `320`-event burst tick, so the important signal is the repeated rate change while the selected operator simultaneously records `Backpressured (max): 85%`. Input and output remaining close also confirms that the job is still making progress rather than silently stalling.

![Unoptimized Flink baseline backpressure and busy time](../../pngs/flink_baseline_burst_pressure_busy_time.png)

**Figure: Unoptimized Flink baseline backpressure time versus busy time.** `backPressuredTimeMsPerSecond` rises from roughly `817` to above `910 ms/s` and remains above `830 ms/s` across the visible interval. Meanwhile, `busyTimeMsPerSecond` falls from just over `100` to about `50-75 ms/s`. This inverse pattern shows that the subtask is spending most of each second blocked by downstream pressure rather than executing useful processing work; the graph overlay independently reports a maximum backpressure of `83%`.

![Unoptimized Flink baseline mailbox latency and input queue](../../pngs/flink_baseline_burst_mailbox_queue.png)

**Figure: Unoptimized Flink baseline mailbox latency and input-buffer queue.** The Netty input queue repeatedly expands from roughly `2` buffers to `15`, drains to about `6`, and then fills to `15` again. At the same time, mailbox p95 latency remains near `370 ms` for most of the interval before recovering to about `100 ms`. The recurring queue fill/drain cycle is direct evidence that burst input is being buffered and processed unevenly rather than flowing at a stable rate.

**Analysis:** `RUNNING` only proves liveness. Taken together, the screenshots show the complete baseline symptom chain: the rolling input/output rate changes, the input queue repeatedly fills, mailbox latency stays elevated, and the operator spends more than `800 ms` of many one-second intervals backpressured. The later optimized capture must replay the same `40 -> 320` workload and reduce backpressure, queue occupancy, and mailbox latency while preserving comparable throughput before claiming improvement.

Code reference:

- [data_generator_e2e_2k.yaml (line 60)](../../../configs/local/data_generator_e2e_2k.yaml#L60): configures 40 normal events per producer tick.
- [data_generator_e2e_2k.yaml (line 66)](../../../configs/local/data_generator_e2e_2k.yaml#L66): triggers a burst every fifth tick.
- [data_generator_e2e_2k.yaml (line 67)](../../../configs/local/data_generator_e2e_2k.yaml#L67): multiplies burst ticks by eight.
- [burst_traffic.py (line 6)](../../../apps/data-platform/data-generator/src/streaming/problems/burst_traffic.py#L6): calculates the per-tick event count.
- [producer.py (line 39)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L39): applies the result in the live loop.
- [flink-baseline-ui-job.yaml (line 100)](../../../infra/k8s/processing-baseline/flink-baseline-ui-job.yaml#L100): identifies the baseline online consumer group used by these screenshots.

#### Late Arrival Problems

The same stress run marks `28%` of newly generated events as late and backdates their event timestamps by `45-180` minutes. The baseline classifier compares each event timestamp with the current Flink watermark and exposes three cumulative counters:

- `late_arrivals_total`: every event for which `event_timestamp <= current_watermark`.
- `accepted_late_events_total`: a late event still inside the configured allowed-lateness/cleanup boundary.
- `too_late_events_total`: a late event beyond that boundary, which must not update the live feature window.

For a single subtask sampled at the same instant, the expected invariant is `late_arrivals_total = accepted_late_events_total + too_late_events_total`.

![Unoptimized Flink baseline late and accepted-late counters](../../pngs/flink_baseline_late_accepted_metrics.png)

**Figure: Unoptimized Flink baseline late-arrival and accepted-late counters.** The Metrics tab on `watermark-lateness-classifier` shows both `late_arrivals_total` and `accepted_late_events_total` increasing in steps while records continue to enter the operator. Each step corresponds to another injected batch crossing the watermark; only the smaller accepted subset remains within allowed lateness. At the same time, the vertex overlay reaches `Backpressured (max): 82%`, linking the event-time problem proof to the pressured baseline run.

![Unoptimized Flink baseline too-late and input counters](../../pngs/flink_baseline_too_late_input_metrics.png)

**Figure: Unoptimized Flink baseline too-late events versus operator input.** The upper chart shows `too_late_events_total` rising past `9,000`; the lower `numRecordsIn` chart rises to approximately `45,000`. Thus a substantial share of processed input is arriving beyond the cleanup boundary rather than contributing safely to the live feature state. The classifier's maximum backpressure also reaches `86%` in this capture.

**Analysis:** these are cumulative counter charts, so their staircase shape is expected and their absolute values include all events since this job attempt started. The two screenshots were taken at different times; endpoint values across them must not be added together. To verify the invariant exactly, switch all three counter cards to **Numeric** and record them at the same instant. The baseline proves the problem exists; improvement is demonstrated only by replaying the same workload against the optimized job and comparing ratios such as `too_late_events_total / numRecordsIn`, together with backpressure and throughput.

**How to reproduce the Flink UI proof:** open the baseline online job, select `watermark-lateness-classifier`, choose **BackPressure** for the pressure capture, then choose **Metrics** and add `late_arrivals_total`, `accepted_late_events_total`, `too_late_events_total`, and `numRecordsIn`. Use **Big** charts for the trend and **Numeric** for an exact before/after table.

Code reference:

- [data_generator_e2e_2k.yaml (line 72)](../../../configs/local/data_generator_e2e_2k.yaml#L72), [data_generator_e2e_2k.yaml (line 74)](../../../configs/local/data_generator_e2e_2k.yaml#L74): configures the 28% late-arrival rate and 45-180 minute delay range.
- [late_arrival.py (line 14)](../../../apps/data-platform/data-generator/src/streaming/problems/late_arrival.py#L14): samples and backdates a late event.
- [problem_pipeline.py (line 38)](../../../apps/data-platform/data-generator/src/streaming/problem_pipeline.py#L38): applies the late-arrival class to new events.
- [event_time.py (line 13)](../../../apps/data-platform/src/features/flink/event_time.py#L13): registers the three shared Flink counters through the operator `MetricGroup`.
- [event_time.py (line 31)](../../../apps/data-platform/src/features/flink/event_time.py#L31): increments and partitions late arrivals into accepted and too-late outcomes.
- [flink-baseline-ui-job.yaml (line 94)](../../../infra/k8s/processing-baseline/flink-baseline-ui-job.yaml#L94): submits the baseline online job used for the UI comparison.

### Develop Stream Processing Script To Handle Streaming Problems

#### Bursty Traffic

**Techniques used:** native tumbling event-time windows, incremental aggregation, Kafka/Flink parallelism, RocksDB state, incremental checkpoints, unaligned checkpoints, and exponential-delay restart. The implementation is split into `event_time.py`, `quality_windows.py`, and `runtime_config.py`; `realtime_stream_job.py` only wires those responsibilities into the production graph.

**Best-practice reference:** [Flink Windows](https://nightlies.apache.org/flink/flink-docs-master/docs/dev/datastream/operators/windows/). Flink describes windows as the core mechanism for splitting an infinite stream into finite buckets for computation. The streaming job applies that pattern by assigning CDC events into fixed event-time quality windows and marking a window as bursty when `event_count >= burst_threshold_event_count`.

**How this minimizes burst pressure:** Kafka is created with four partitions so the deployment can scale source parallelism when load requires it. The fixed proof topology remains two parallelism-one jobs on two one-slot TaskManagers: one route ends at Redis and the other at PostgreSQL. Because this layout is held constant, the processing optimization is evaluated in the graph itself. The quality window uses `AggregateFunction` rather than buffering every event until the window closes. Unaligned checkpoints avoid waiting for all in-flight buffers during backpressure, and exponential-delay restart prevents tight crash loops.

Code reference:

- [quality_windows.py (line 89)](../../../apps/data-platform/src/features/flink/quality_windows.py#L89): implements the native incremental `AggregateFunction`.
- [quality_windows.py (line 102)](../../../apps/data-platform/src/features/flink/quality_windows.py#L102): increments the event counter without retaining the full window contents.
- [quality_windows.py (line 124)](../../../apps/data-platform/src/features/flink/quality_windows.py#L124): derives `is_bursty` from the configured event threshold.
- [realtime_stream_job.py (line 908)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L908): creates the native `TumblingEventTimeWindows` operator.
- [realtime_stream_job.py (line 911)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L911): attaches the incremental aggregate to that window.
- [values.yaml (line 100)](../../../infra/helm/recsys-data-platform/values.yaml#L100): declares four Kafka partitions.
- [values-gcp.yaml (line 53)](../../../infra/helm/recsys-data-platform/values-gcp.yaml#L53): declares the fixed two-TaskManager proof topology.
- [values-gcp.yaml (line 65)](../../../infra/helm/recsys-data-platform/values-gcp.yaml#L65): gives each TaskManager one slot; with two parallelism-one jobs, one route serves Redis and the other serves PostgreSQL.
- [values-gcp.yaml (line 73)](../../../infra/helm/recsys-data-platform/values-gcp.yaml#L73): runs each GCP realtime Flink job at parallelism one.
- [values.yaml (line 131)](../../../infra/helm/recsys-data-platform/values.yaml#L131): selects RocksDB for production state.
- [values.yaml (line 132)](../../../infra/helm/recsys-data-platform/values.yaml#L132): enables incremental RocksDB checkpoints.
- [values.yaml (line 212)](../../../infra/helm/recsys-data-platform/values.yaml#L212): enables unaligned checkpoints.
- [kafka-topic-init.yaml (line 27)](../../../infra/helm/recsys-data-platform/templates/kafka-topic-init.yaml#L27): creates the CDC topic with the requested partition count.
- [kafka-topic-init.yaml (line 30)](../../../infra/helm/recsys-data-platform/templates/kafka-topic-init.yaml#L30): raises the partition count on an existing topic when necessary.
- [kafka-redis-flink.yaml (line 271)](../../../infra/helm/recsys-data-platform/templates/kafka-redis-flink.yaml#L271): configures task slots, RocksDB/incremental state, exponential-delay restart, and checkpoint storage for JobManager.
- [kafka-redis-flink.yaml (line 330)](../../../infra/helm/recsys-data-platform/templates/kafka-redis-flink.yaml#L330): applies the matching runtime properties to TaskManager.

#### Late Arrival

**Techniques used:** source-level event timestamps, bounded-out-of-orderness watermarks, idle-partition detection, watermark alignment, native event-time tumbling windows, native allowed lateness, native late-data `OutputTag`, PostgreSQL DLQ, and state TTL.

**Best-practice reference:** [Flink Generating Watermarks](https://nightlies.apache.org/flink/flink-docs-master/docs/dev/datastream/event-time/generating_watermarks/). Flink defines `WatermarkStrategy` as the combination of timestamp assignment and watermark generation, documents Python `for_bounded_out_of_orderness(...)`, recommends applying watermark strategy directly at the source when possible, documents `.with_idleness(...)` for idle source/partition handling, and documents `.with_watermark_alignment(...)` for keeping fast sources from moving too far ahead of slow ones.

**Best-practice reference:** [Flink Windows - Allowed Lateness and Side Output Late Data](https://nightlies.apache.org/flink/flink-docs-master/docs/dev/datastream/operators/windows/#allowed-lateness). The production graph now calls Flink's native `.allowed_lateness(...)` and `.side_output_late_data(...)` APIs. The separate watermark classifier mirrors the window cleanup boundary so a too-late event cannot update Redis or PostgreSQL after Flink routes it to the late-data branch.

**Best-practice reference:** [Flink Working with State - State Time-To-Live](https://nightlies.apache.org/flink/flink-docs-master/docs/dev/datastream/fault-tolerance/state/#state-time-to-live-ttl). Flink supports TTL on keyed state descriptors so dedup/history/window state does not grow forever. This repo enables TTL on dedup, user-history, item-history, and quality-window state.

**How this minimizes late-data errors:** an event behind the watermark can still update its window until `window_end + allowed_lateness`; after that cleanup boundary, Flink routes it through the native side output and the feature branch rejects it. The PostgreSQL DLQ preserves the rejected event for a future replay/backfill workflow. This job implements the capture point, but it does not claim that an automated reconciliation/backfill job already exists.

Code reference:

- [realtime_stream_job.py (line 876)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L876): creates the bounded-out-of-orderness watermark strategy.
- [realtime_stream_job.py (line 878)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L878): assigns `event_timestamp` as native event time.
- [realtime_stream_job.py (line 880)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L880): marks idle Kafka partitions so they do not stall the global watermark.
- [realtime_stream_job.py (line 882)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L882): enables watermark alignment for uneven Kafka partitions.
- [realtime_stream_job.py (line 655)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L655): reads Flink's current operator watermark.
- [event_time.py (line 30)](../../../apps/data-platform/src/features/flink/event_time.py#L30): calculates the event's event-time window end.
- [event_time.py (line 33)](../../../apps/data-platform/src/features/flink/event_time.py#L33): uses `window_end + allowed_lateness` as the too-late boundary.
- [realtime_stream_job.py (line 905)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L905): declares the native late-event `OutputTag`.
- [realtime_stream_job.py (line 909)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L909): configures native allowed lateness in milliseconds.
- [realtime_stream_job.py (line 910)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L910): attaches native late-data side output to the window.
- [realtime_stream_job.py (line 922)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L922): reads the native late-data side output.
- [realtime_stream_job.py (line 925)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L925): writes too-late events to the PostgreSQL DLQ.
- [realtime_stream_job.py (line 928)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L928): prevents too-late events from entering the feature path.
- [runtime_config.py (line 13)](../../../apps/data-platform/src/features/flink/runtime_config.py#L13): builds native state TTL.
- [runtime_config.py (line 18)](../../../apps/data-platform/src/features/flink/runtime_config.py#L18): enables TTL on each keyed-state descriptor.
- [values.yaml (line 196)](../../../infra/helm/recsys-data-platform/values.yaml#L196): sets the production watermark delay.
- [values.yaml (line 197)](../../../infra/helm/recsys-data-platform/values.yaml#L197): sets the allowed-lateness interval.
- [values.yaml (line 199)](../../../infra/helm/recsys-data-platform/values.yaml#L199): enables watermark alignment in production.

#### Failure Recovery And Sink Replay

**Techniques used:** Flink EXACTLY_ONCE checkpoint mode for Kafka offsets and operator state, retained externalized checkpoints, one in-flight checkpoint, checkpoint timeout/failure tolerance, optional unaligned checkpoints, and idempotent external writes.

**Best-practice reference:** [Apache Flink 1.19 - Checkpointing](https://nightlies.apache.org/flink/flink-docs-release-1.19/docs/dev/datastream/fault-tolerance/checkpointing/). Flink checkpoints recover operator state and source positions with failure-free execution semantics. The production job follows the documented PyFlink pattern by enabling checkpointing, selecting `CheckpointingMode.EXACTLY_ONCE`, limiting concurrent checkpoints to one, retaining externalized checkpoints, and optionally enabling unaligned checkpoints for backpressured execution.

**Delivery-guarantee reference:** [Apache Flink - Exactly Once End-to-end](https://nightlies.apache.org/flink/flink-docs-stable/docs/learn-flink/fault_tolerance/#exactly-once-end-to-end). Flink distinguishes exactly-once effects on managed state from exactly-once delivery to external systems. End-to-end exactly-once requires a replayable source and a transactional or idempotent sink. Kafka and Flink managed state satisfy the checkpointed processing boundary here; the synchronous Python Redis and PostgreSQL writers use idempotent writes rather than participating in a Flink two-phase-commit transaction.

Flink's checkpoint mode does not make a synchronous Python PostgreSQL/Redis client a two-phase-commit sink. Therefore, this implementation states the boundary precisely: Kafka offsets and Flink keyed state recover exactly once; replayed external writes are made idempotent. PostgreSQL upserts by `source_event_id`, the DLQ ignores duplicate `(event_id, reason)`, and Redis uses an atomic Lua compare-and-set so an older replay cannot overwrite a newer feature payload.

Code reference:

- [runtime_config.py (line 28)](../../../apps/data-platform/src/features/flink/runtime_config.py#L28): selects Flink `CheckpointingMode.EXACTLY_ONCE`.
- [runtime_config.py (line 29)](../../../apps/data-platform/src/features/flink/runtime_config.py#L29): enforces a minimum pause between checkpoints.
- [runtime_config.py (line 30)](../../../apps/data-platform/src/features/flink/runtime_config.py#L30): bounds checkpoint duration.
- [runtime_config.py (line 31)](../../../apps/data-platform/src/features/flink/runtime_config.py#L31): limits concurrent checkpoints to one.
- [runtime_config.py (line 32)](../../../apps/data-platform/src/features/flink/runtime_config.py#L32): configures tolerated checkpoint failures.
- [runtime_config.py (line 33)](../../../apps/data-platform/src/features/flink/runtime_config.py#L33): retains externalized checkpoints on cancellation.
- [runtime_config.py (line 35)](../../../apps/data-platform/src/features/flink/runtime_config.py#L35): enables unaligned checkpoints for backpressured streams.
- [realtime_stream_job.py (line 1088)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L1088): applies checkpoint configuration before building/executing the job.
- [realtime_stream_job.py (line 279)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L279): carries the source event id into user-sequence rows.
- [realtime_stream_job.py (line 299)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L299): carries the source event id into user-aggregate rows.
- [realtime_stream_job.py (line 318)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L318): carries the source event id into item-feature rows.
- [postgres_offline_store.py (line 194)](../../../apps/data-platform/src/feature_store/postgres_offline_store.py#L194): creates the partial unique index on `source_event_id`.
- [postgres_offline_store.py (line 204)](../../../apps/data-platform/src/feature_store/postgres_offline_store.py#L204): creates the DLQ uniqueness constraint.
- [postgres_offline_store.py (line 250)](../../../apps/data-platform/src/feature_store/postgres_offline_store.py#L250): upserts replayed feature events.
- [postgres_offline_store.py (line 253)](../../../apps/data-platform/src/feature_store/postgres_offline_store.py#L253): ignores a replayed DLQ event.
- [online_writer.py (line 35)](../../../apps/data-platform/src/feature_store/online_writer.py#L35): compares the stored and incoming `updated_at` values atomically in Redis Lua.
- [online_writer.py (line 39)](../../../apps/data-platform/src/feature_store/online_writer.py#L39): atomically writes the accepted latest payload with TTL.
- [online_writer.py (line 51)](../../../apps/data-platform/src/feature_store/online_writer.py#L51): executes the Lua compare-and-set through Redis.
- [values.yaml (line 209)](../../../infra/helm/recsys-data-platform/values.yaml#L209): configures checkpoint minimum pause.
- [values.yaml (line 210)](../../../infra/helm/recsys-data-platform/values.yaml#L210): configures checkpoint timeout.
- [values.yaml (line 212)](../../../infra/helm/recsys-data-platform/values.yaml#L212): enables unaligned checkpoints in production.

#### Production Runtime Routing

The deployed streaming layout is `Kafka CDC -> Flink -> PostgreSQL Feast offline store` and `Kafka CDC -> Flink -> Redis online store`. Iceberg remains part of the batch lakehouse, but it is not the production streaming offline-store sink.

Code reference:

- [values.yaml (line 171)](../../../infra/helm/recsys-data-platform/values.yaml#L171): selects PostgreSQL as the realtime offline sink.
- [realtime-flink-consumer.yaml (line 77)](../../../infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml#L77): disables the offline branch in the Redis online-store consumer.
- [realtime-flink-consumer.yaml (line 169)](../../../infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml#L169): enables the offline branch in the PostgreSQL consumer.
- [realtime-flink-consumer.yaml (line 170)](../../../infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml#L170): disables Redis writes in the PostgreSQL consumer.
- [realtime-flink-consumer.yaml (line 171)](../../../infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml#L171): passes the configured PostgreSQL sink selection.


### View Flink UI To Show Problems Have Been Minimized

#### Bursty Traffic

The captures below select the optimized online job (`2e9bc3e8873892d9d314416267891ce4`) and its `watermark-lateness-classifier` under the same fixed two-TaskManager layout described above. The rate, pressure, mailbox, and queue charts can be compared as runtime observations. The cumulative-counter charts contain a metric-series reset inside the displayed time range; because Flink counters are monotonic within one execution attempt, the descending segment is a UI discontinuity and must not be interpreted as fewer late events.

![Optimized online Flink low-backpressure snapshot](../../pngs/flink_optimized_online_backpressure_low.png)

**Figure: optimized online classifier at a transient LOW-backpressure snapshot.** The selected subtask reports `37%` backpressure, `45%` idle, and `18%` busy while the job remains `RUNNING 6/6`. This is below the baseline screenshot's `66% HIGH` sample, but it is a point-in-time result rather than proof that pressure disappeared: the longer metric captures below still reach `85-88%` maximum pressure.

![Optimized online Flink throughput rates](../../pngs/flink_optimized_online_throughput_rates.png)

**Figure: optimized online input and output rates.** Classifier input varies from approximately `25` to `41 records/s`; output varies from roughly `41` to `68 records/s`. Output is higher because the classifier fans one logical event into the native quality-window and feature branches, so it is not a duplicate-event count. Both curves remain active and confirm end-to-end liveness, but input throughput is below the baseline capture's `54-69 records/s`; this image therefore supports continuity, not a throughput-improvement claim.

![Optimized online Flink backpressure and busy time](../../pngs/flink_optimized_online_pressure_busy_time.png)

**Figure: optimized online backpressure time versus busy time.** `backPressuredTimeMsPerSecond` ranges from about `720` to `915 ms/s`, while useful busy time ranges from approximately `30` to `68 ms/s`. The lower part of the pressure curve is below many baseline samples above `830 ms/s`, but the ranges overlap and the graph overlay still reaches `88%`. This image does not prove that backpressure was eliminated; it shows that the Redis path remains constrained under the current `40 -> 320` burst workload.

![Optimized online Flink mailbox latency and input queue](../../pngs/flink_optimized_online_mailbox_queue.png)

**Figure: optimized online mailbox latency and input queue.** Mailbox p95 falls from roughly `230 ms` to about `25 ms`, with one short spike below `100 ms`; this is below the baseline capture's sustained approximately `370 ms`. Input queue occupancy is mainly around `1-12` buffers with a short spike to `13`, compared with repeated baseline saturation at `15`. This is the clearest observed improvement in the captured metrics: scheduling responsiveness and typical queue occupancy are better even though backpressure remains.

**Analysis:** the optimized run proves a narrower result than a graph-only comparison would suggest. With the two-TaskManager deployment treated as fixed, the measurable improvement is lower mailbox latency and lower typical input-buffer occupancy in the optimized graph. The screenshots do not prove higher throughput or elimination of backpressure: optimized input rate is lower and pressure still peaks near the baseline range. The remaining constraint is downstream online-store work and accumulated Kafka backlog, not an unbounded window buffer.

#### Late Arrival

![Optimized online Flink late and accepted-late counters](../../pngs/flink_optimized_online_late_accepted_metrics.png)

**Figure: optimized online late-arrival and accepted-late counters with a metric-series reset.** Both counters are cumulative and cannot decrement within one execution attempt, so the apparent decline is a UI interpolation across a reset in the displayed metric series, not a reduction in late events. At the right edge, both counters resume increasing as newly generated delayed events cross the watermark. This verifies that the metrics remain active, but the descending segment must not be used to calculate an optimization ratio.

![Optimized online Flink too-late and input counters](../../pngs/flink_optimized_online_too_late_input_metrics.png)

**Figure: optimized online too-late events versus classifier input with a metric-series reset.** `too_late_events_total` and `numRecordsIn` show the same reset discontinuity and then resume rising together. This confirms that post-cleanup events continue to be classified while records flow through the optimized graph. Exact validation must use Numeric values sampled simultaneously and enforce `late_arrivals_total = accepted_late_events_total + too_late_events_total`; the descending chart segments are not event-loss or optimization evidence.


### Window Processing

The production graph classifies every deduplicated event against Flink's current watermark before the native window operator. The classifier registers three cumulative Flink counters through the operator's `MetricGroup`, then partitions every late arrival into exactly one accepted or too-late bucket:

```python
class LateArrivalMetricCounters:
    def __init__(self, metric_group):
        self.late_arrivals_total = metric_group.counter("late_arrivals_total")
        self.accepted_late_events_total = metric_group.counter("accepted_late_events_total")
        self.too_late_events_total = metric_group.counter("too_late_events_total")
        for counter in (
            self.late_arrivals_total,
            self.accepted_late_events_total,
            self.too_late_events_total,
        ):
            counter.inc()
            counter.dec()  # Preserve a neutral bootstrap value across backends.

    def record(self, is_late, is_too_late):
        if not is_late:
            return
        self.late_arrivals_total.inc()
        if is_too_late:
            self.too_late_events_total.inc()
        else:
            self.accepted_late_events_total.inc()


class MarkEventTimeStatus(KeyedProcessFunction):
    def open(self, runtime_context):
        self.late_arrival_metrics = LateArrivalMetricCounters(
            runtime_context.get_metrics_group()
        )

    def process_element(self, event, ctx):
        watermark_ms = int(ctx.timer_service().current_watermark())
        late_by_seconds, is_late, is_too_late = event_time_status(
            event,
            watermark_ms,
            args.allowed_lateness_seconds,
            args.quality_window_seconds,
        )
        self.late_arrival_metrics.record(is_late, is_too_late)
        # The marked event continues into the native window and feature branches.


quality_rows = (
    marked.key_by(lambda event: args.topic)
    .window(TumblingEventTimeWindows.of(Time.seconds(args.quality_window_seconds)))
    .allowed_lateness(args.allowed_lateness_seconds * 1000)
    .side_output_late_data(late_event_tag)
    .aggregate(native_quality_window_aggregate(args))
)
```

In the Flink UI, open the `watermark-lateness-classifier` vertex, select **Metrics**, and search for `late_arrivals_total`, `accepted_late_events_total`, and `too_late_events_total`. The runtime IDs are prefixed with the operator name, for example `watermark-lateness-classifier.accepted_late_events_total`. PyFlink publishes custom counters after a metric bundle and Beam may omit a counter whose cumulative value has never become non-zero. For the before/after proof, the GCP profile therefore uses `allowed_lateness=3600s` with the generator's 45–180 minute delay range, producing both accepted-late and post-cleanup events so all three real counters appear without adding a sentinel offset. The current GCP profile runs each realtime job at parallelism one, so each job exposes one subtask value. At higher parallelism, sum the subtask counters before comparing runs. Counters are cumulative for one job attempt and reset after a fresh deployment or restart.

The counter invariant is `late_arrivals_total = accepted_late_events_total + too_late_events_total`. Use the same input, Kafka starting offsets, duration, watermark delay, allowed lateness, and parallelism for baseline and optimized captures. `late_arrivals_total` measures the input condition and should remain comparable; optimization evidence is a higher accepted-late ratio, a lower too-late ratio, and lower backpressure/mailbox latency at comparable throughput. If only state/window efficiency changes, the late-event ratios may remain stable and the improvement should instead be claimed from the runtime pressure metrics.

The `streaming-quality-window-metrics` operator also writes structured runtime log records with `window_start`, `window_end`, `event_count`, `late_event_count`, `duplicate_event_count`, `max_late_by_seconds`, and `is_bursty`. In the PostgreSQL production-sink branch, quality windows are logged rather than persisted to a `streaming_quality_windows` table. Too-late records are additionally verified through `native-late-events-side-output` and `stream_late_events_dlq`.

**How the production window works:** the deduplicated event stream is marked against Flink's current watermark, keyed by topic, and passed into native `TumblingEventTimeWindows`. Flink owns window lifecycle and cleanup. `allowed_lateness` keeps a fired window open for permitted late updates; events arriving after cleanup are emitted through the native `OutputTag` side output. A custom incremental `AggregateFunction` stores only counters and the maximum lateness, rather than buffering all events.

For each accepted window event, the accumulator increments `event_count`, `late_event_count`, and `duplicate_event_count`, retains `max_late_by_seconds`, and emits `is_bursty` when the configured threshold is reached. `late_events_dropped` and `side_output_late_events` in the main window row remain zero because events that are too late are no longer members of that window; the separate side-output/DLQ branch is the authoritative record for dropped late events.

**What this proves:** the current production path uses Flink-native event-time windows, allowed lateness, cleanup, and side output. `StreamQualityTracker` remains only as a pure-Python unit-test/local-diagnostic helper; it is not the production window operator.

Code reference:

- [event_time.py (line 9)](../../../apps/data-platform/src/features/flink/event_time.py#L9): registers the three custom Flink counters.
- [event_time.py (line 21)](../../../apps/data-platform/src/features/flink/event_time.py#L21): enforces the accepted-versus-too-late counter partition.
- [event_time.py (line 41)](../../../apps/data-platform/src/features/flink/event_time.py#L41): classifies lateness against the watermark and `window_end + allowed_lateness` cleanup boundary.
- [realtime_stream_job.py (line 653)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L653): obtains the operator `MetricGroup` and records every event-time classification.
- [realtime_stream_job.py (line 905)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L905): keys and classifies each event against the native watermark.
- [realtime_stream_job.py (line 911)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L911): keys quality aggregation by Kafka topic.
- [realtime_stream_job.py (line 912)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L912): defines the native tumbling event-time window.
- [realtime_stream_job.py (line 913)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L913): retains fired windows for configured allowed lateness.
- [realtime_stream_job.py (line 914)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L914): routes post-cleanup late records to native side output.
- [quality_windows.py (line 93)](../../../apps/data-platform/src/features/flink/quality_windows.py#L93): initializes constant-size window counters.
- [quality_windows.py (line 103)](../../../apps/data-platform/src/features/flink/quality_windows.py#L103): counts accepted late updates.
- [quality_windows.py (line 104)](../../../apps/data-platform/src/features/flink/quality_windows.py#L104): counts duplicate markers.
- [quality_windows.py (line 105)](../../../apps/data-platform/src/features/flink/quality_windows.py#L105): keeps only the maximum lateness value.
- [quality_windows.py (line 124)](../../../apps/data-platform/src/features/flink/quality_windows.py#L124): computes the burst flag.

## Production Integration Proof

### Spark Batch Job Integrated Into Airflow Pipeline

Spark batch processing is integrated into the Airflow DAGs through native Spark-on-Kubernetes submission rather than a permanently running Spark cluster, a local Spark process, or a Spark Operator `SparkApplication` resource. The shared `spark_native_submit()` helper builds the same production `spark-submit` contract for the DP2 and DP3 Spark tasks. See [rubric_data_pipeline_dags.py (line 86)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L86) for the `KubernetesPodOperator` wrapper and [rubric_data_pipeline_dags.py (line 105)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L105) for the shared submission helper.

#### Native Spark-On-Kubernetes Execution Flow

The integration uses the following reference-backed execution path:

| Step | Execution flow | Code reference |
|---:|---|---|
| 1 | The Airflow scheduler loads and schedules the rubric DAGs. | [airflow.yaml (line 67)](../../../infra/helm/recsys-data-platform/templates/airflow.yaml#L67) declares the scheduler Deployment and [line 92](../../../infra/helm/recsys-data-platform/templates/airflow.yaml#L92) starts `airflow scheduler`. |
| 2 | `KubernetesPodOperator` creates a temporary Spark submission pod for the Airflow task. | [rubric_data_pipeline_dags.py (line 86)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L86) constructs the operator; [line 99](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L99) deletes the temporary pod after completion. |
| 3 | The submission pod runs `spark-submit` against the Kubernetes API. | [rubric_data_pipeline_dags.py (line 119)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L119) invokes `spark-submit`, and [line 120](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L120) selects the in-cluster Kubernetes API master. |
| 4 | Kubernetes creates a separate Spark driver pod because submission uses cluster deploy mode. | [rubric_data_pipeline_dags.py (line 121)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L121) sets `--deploy-mode cluster`; [line 123](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L123) selects the driver namespace and [line 124](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L124) selects the Spark container image. |
| 5 | The driver requests, monitors, and removes executor pods according to the Spark allocation policy. | [rubric_data_pipeline_dags.py (line 126)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L126) assigns the driver's Kubernetes service account; [rbac.yaml (line 7)](../../../infra/helm/recsys-data-platform/templates/rbac.yaml#L7) permits pod lifecycle operations; [rubric_data_pipeline_dags.py (line 139)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L139) through [line 146](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L146) define dynamic executor allocation. |
| 6 | The submission pod waits until the driver application succeeds or fails. | [rubric_data_pipeline_dags.py (line 127)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L127) sets `spark.kubernetes.submission.waitAppCompletion=true`; [line 130](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L130) reports application state every five seconds. |
| 7 | Airflow marks `ingest_stage` successful only after Spark completes, then releases `validate_stage`. | [rubric_data_pipeline_dags.py (line 244)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L244) enforces the DP2 dependency and [line 265](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L265) enforces the DP3 dependency. |

The submission command therefore keeps the Airflow task attached to the real Spark application outcome instead of treating a successful submission request as job completion. The temporary submission pod, driver, and executors use the Spark image and run in namespace `recsys-dataflow`; driver and executor pods are placed on the `cpu-services` node pool by [rubric_data_pipeline_dags.py (line 133)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L133). ConfigMap values, object-store settings, PostgreSQL settings, and Kubernetes Secrets are propagated from the `KubernetesPodOperator` environment into both the driver and executors.

#### How The Shared Spark Contract Is Applied To Airflow Pipelines

The GCP Helm values are rendered into `recsys-data-platform-config`. Each `KubernetesPodOperator` imports that ConfigMap and the platform Secret with `env_from`. The shared helper then converts the environment variables into `spark-submit --conf` settings. This creates one configuration path:

```text
values-gcp.yaml
  -> Helm ConfigMap
  -> KubernetesPodOperator submission pod environment
  -> spark_native_submit()
  -> Spark driver and executor configuration
```

Implementation reference: [values-gcp.yaml (line 27)](../../../infra/helm/recsys-data-platform/values-gcp.yaml#L27) defines the GCP Spark values; [configmap.yaml (line 51)](../../../infra/helm/recsys-data-platform/templates/configmap.yaml#L51) renders them as environment variables; [rubric_data_pipeline_dags.py (line 65)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L65) imports the ConfigMap and Secret; and [rubric_data_pipeline_dags.py (line 107)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L107) forwards runtime settings to the driver and executors.

Terraform applies `values-gcp.yaml` when initially installing the data-platform Helm release. Jenkins component deployment also sets the GCP Spark resource and dynamic-allocation values explicitly, so later component updates do not silently fall back to the local profile. See [recsys_services.tf (line 116)](../../../infra/terraform/gcp/recsys_services.tf#L116) for the Terraform Helm release and [component_deploy.sh (line 257)](../../../jenkins/scripts/component_deploy.sh#L257) plus [line 271](../../../jenkins/scripts/component_deploy.sh#L271) for the Jenkins Helm update and `spark.dynamicAllocation.enabled` override.

#### DP2: Spark Bronze To Silver Processing

In DAG `recsys_dp2_bronze_to_silver_gold`, both Airflow stages submit Spark applications. The `ingest_stage` reads the Bronze lakehouse data produced by DP1, normalizes timestamps and compatible schema changes, rejects duplicate or invalid behavior events, builds order facts and product SCD data, and writes the curated datasets as `silver_*` lakehouse tables. The following `validate_stage` reads every expected curated table with Spark and fails the DAG when any table is empty.

Implementation reference: [rubric_data_pipeline_dags.py (line 180)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L180) and [line 186](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L186) build the two Spark commands; [line 226](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L226) and [line 244](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L244) declare the DAG and enforce `ingest_stage >> validate_stage`; [dp2_silver_gold_entrypoint.py (line 20)](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py#L20) and [line 35](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py#L35) implement ingestion and validation.

The `silver_gold` suffix remains in the historical DAG and Python identifiers, but DP2 physically writes only the `silver_*` Iceberg layer.

![DP2 Airflow DAG proof](../../pngs/dp2_airflow_ui.png)

**Figure: DP2 Spark integration in Airflow.** The Airflow Graph view shows the ordered `ingest_stage -> validate_stage` workflow in DAG `recsys_dp2_bronze_to_silver_gold`. Both green nodes prove that Spark completed the Bronze-to-Silver transformation and subsequently verified the resulting curated lakehouse tables.

#### DP3: Spark Offline Feature Engineering

In DAG `recsys_dp3_offline_feature_table`, the `ingest_stage` submits the production Spark batch feature job. Spark builds the clean input frames, computes `user_sequence_features`, `user_aggregate_features`, `item_features`, ranking labels, and the BST training dataset, writes the feature outputs to the feature lakehouse namespace, and exports the Feast-facing tables to PostgreSQL. PostgreSQL is the configured Feast offline store; Apache Iceberg remains the upstream lakehouse and feature-storage layer.

Implementation reference: [rubric_data_pipeline_dags.py (line 157)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L157) builds the DP3 Spark command; [line 247](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L247) and [line 254](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L254) attach it to the DP3 `ingest_stage`; [spark_batch_entrypoint.py (line 179)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L179), [line 186](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L186), and [line 197](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L197) read Silver, compute/write feature outputs, and export PostgreSQL tables.

The DP3 `validate_stage` does not perform feature engineering. It connects to PostgreSQL after Spark finishes and runs row-count checks against every expected offline-store table. Therefore, the count checks are completion validation only; the actual transformations and feature calculations happen in the preceding Spark `ingest_stage`.

Implementation reference: [rubric_data_pipeline_dags.py (line 259)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L259) runs the non-Spark validation task after ingestion, [line 265](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L265) enforces the ordering, and [governance_contracts.py (line 134)](../../../apps/data-platform/src/validate/governance_contracts.py#L134) implements PostgreSQL offline-store validation.

![DP3 Airflow DAG proof](../../pngs/dp3_airflow_ui.png)

**Figure: DP3 Spark integration in Airflow.** The Airflow Graph view shows `ingest_stage -> validate_stage` in DAG `recsys_dp3_offline_feature_table`. The successful Spark ingest node proves that feature computation and PostgreSQL export completed, while the successful validation node proves that the resulting Feast offline-store tables contain data.

#### Spark Scaling On GCP

The GCP profile enables `spark.dynamicAllocation.enabled=true` with Kubernetes-compatible shuffle tracking. Spark 3.5 uses `spark.dynamicAllocation.shuffleTracking.enabled=true`, so executor removal does not require an external shuffle service. The GCP switch and bounds are defined in [values-gcp.yaml](../../../infra/helm/recsys-data-platform/values-gcp.yaml), rendered by [configmap.yaml](../../../infra/helm/recsys-data-platform/templates/configmap.yaml), and passed to every native Airflow Spark application by [rubric_data_pipeline_dags.py](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py). This follows the [Apache Spark 3.5 dynamic resource allocation requirements](https://spark.apache.org/docs/3.5.7/job-scheduling.html#dynamic-resource-allocation). The configured policy is:

| Setting | GCP value | Behavior |
|---|---:|---|
| `spark.dynamicAllocation.minExecutors` | `1` | Keeps one executor available while the Spark application is active. |
| `spark.dynamicAllocation.initialExecutors` | `1` | Starts each application with one executor. |
| `spark.dynamicAllocation.maxExecutors` | `4` | Caps application-level horizontal scaling at four executor pods. |
| `spark.dynamicAllocation.schedulerBacklogTimeout` | `1s` | Requests another executor after tasks remain queued for one second. |
| `spark.dynamicAllocation.sustainedSchedulerBacklogTimeout` | `1s` | Continues requesting executors while the task backlog persists. |
| `spark.dynamicAllocation.executorIdleTimeout` | `60s` | Removes an idle executor after 60 seconds, down to the minimum. |

Each GCP executor is configured with one Spark core, `4g` heap, and `1g` memory overhead; the driver uses one core, `2g` heap, and `768m` overhead. `spark.sql.shuffle.partitions=16` supplies enough task partitions for more than one executor to work concurrently. These GCP values are declared in [values-gcp.yaml (line 27)](../../../infra/helm/recsys-data-platform/values-gcp.yaml#L27). The base/local Helm profile leaves dynamic allocation disabled and keeps the previous single-executor behavior for lightweight deterministic runs, as shown in [values.yaml (line 80)](../../../infra/helm/recsys-data-platform/values.yaml#L80) and [line 92](../../../infra/helm/recsys-data-platform/values.yaml#L92).

Spark executor scaling and Kubernetes node scaling are separate control loops. Dynamic allocation changes the number of executor pods between one and four according to the Spark task backlog. If the `cpu-services` nodes cannot place those pods, the GKE Cluster Autoscaler can grow the CPU node pool from its configured minimum of two nodes to its maximum of five. When executors become idle, Spark releases them first; the GKE autoscaler can later remove unused nodes. The node autoscaler never decides how many Spark executors an application needs. The CPU node-pool autoscaler is implemented in [gke.tf (line 97)](../../../infra/terraform/gcp/gke.tf#L97); its default minimum and maximum are defined in [variables.tf (line 79)](../../../infra/terraform/gcp/variables.tf#L79) and [line 85](../../../infra/terraform/gcp/variables.tf#L85).

Each rubric DAG still uses `max_active_runs=1`, and DP2/DP3 stage dependencies remain sequential. Dynamic allocation therefore increases or decreases parallelism inside the active Spark application; it does not create overlapping Airflow runs or bypass validation ordering. DP2 declares the run limit and dependency at [rubric_data_pipeline_dags.py (line 230)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L230) and [line 244](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L244); DP3 does the same at [line 251](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L251) and [line 265](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L265).
