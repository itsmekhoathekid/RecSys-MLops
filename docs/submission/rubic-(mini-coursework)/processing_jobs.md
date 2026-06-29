# Processing Jobs

This document covers the before/after optimization evidence for the rubric section:

- Spark offline processing job: skew, high cardinality, schema evolution, duplicate/offline data quality, and Airflow integration.
- Flink streaming processing job: burst traffic, late arrival, duplicates, window processing, and feature-store integration.

## Evidence Files

Code and config:

- [apps/data-platform/src/processing_jobs/benchmark.py](../../../apps/data-platform/src/processing_jobs/benchmark.py): deterministic local benchmark for before/after verification.
- [configs/local/processing_jobs_spark_baseline.yaml](../../../configs/local/processing_jobs_spark_baseline.yaml): Spark baseline version.
- [configs/local/processing_jobs_spark_optimized.yaml](../../../configs/local/processing_jobs_spark_optimized.yaml): Spark optimized version.
- [configs/local/processing_jobs_flink_baseline.yaml](../../../configs/local/processing_jobs_flink_baseline.yaml): Flink baseline version.
- [configs/local/processing_jobs_flink_optimized.yaml](../../../configs/local/processing_jobs_flink_optimized.yaml): Flink optimized version.
- [apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py line 196](../../../apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py#196): Spark batch feature job in Airflow.
- [apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py line 214](../../../apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py#214): Airflow checks the running Flink feature-store job.
- [infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml line 25](../../../infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml#25): Flink streaming job deployment.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 637](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#637): streaming quality window processor.

Generated local benchmark outputs:

- `reports/processing_jobs/spark_baseline.json`
- `reports/processing_jobs/spark_optimized.json`
- `reports/processing_jobs/spark_comparison.json`
- `reports/processing_jobs/flink_baseline.json`
- `reports/processing_jobs/flink_optimized.json`
- `reports/processing_jobs/flink_comparison.json`

## Run And Verify

Run all local before/after checks:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

PYTHONPATH=apps/data-platform/src uv run python -m processing_jobs.benchmark run \
  --config configs/local/processing_jobs_spark_baseline.yaml \
  --output-dir reports/processing_jobs \
  --name spark_baseline

PYTHONPATH=apps/data-platform/src uv run python -m processing_jobs.benchmark run \
  --config configs/local/processing_jobs_spark_optimized.yaml \
  --output-dir reports/processing_jobs \
  --name spark_optimized

PYTHONPATH=apps/data-platform/src uv run python -m processing_jobs.benchmark compare \
  --baseline reports/processing_jobs/spark_baseline.json \
  --optimized reports/processing_jobs/spark_optimized.json \
  --output-dir reports/processing_jobs \
  --name spark_comparison

PYTHONPATH=apps/data-platform/src uv run python -m processing_jobs.benchmark run \
  --config configs/local/processing_jobs_flink_baseline.yaml \
  --output-dir reports/processing_jobs \
  --name flink_baseline

PYTHONPATH=apps/data-platform/src uv run python -m processing_jobs.benchmark run \
  --config configs/local/processing_jobs_flink_optimized.yaml \
  --output-dir reports/processing_jobs \
  --name flink_optimized

PYTHONPATH=apps/data-platform/src uv run python -m processing_jobs.benchmark compare \
  --baseline reports/processing_jobs/flink_baseline.json \
  --optimized reports/processing_jobs/flink_optimized.json \
  --output-dir reports/processing_jobs \
  --name flink_comparison
```

Verify with tests:

```bash
PYTHONPATH=apps/data-platform/src:apps/data-platform/data-generator/src \
  uv run pytest tests/unit/data_platform/test_processing_jobs_benchmark.py -q
```

## Spark Offline Job

### Baseline

Baseline behavior:

- Reads skewed offline events.
- Drops old-schema rows that do not have the evolved `device_type` column.
- Writes duplicate `event_id` records.
- Aggregates each product by repeatedly scanning the event list.
- Keeps raw high-cardinality `campaign_id` values.
- Does not salt the hot product key before shuffle-like aggregation.

Baseline result from `reports/processing_jobs/spark_baseline.json`:

| Metric | Value |
| --- | ---: |
| Input rows | 12,360 |
| Rows used | 6,797 |
| Schema evolution rows dropped | 5,563 |
| Duplicate rows written | 197 |
| Raw campaign cardinality | 2,311 |
| Max partition ratio | 5.0458 |
| Operation count | 13,383,293 |
| Duration | 693.291 ms |

### Optimized

Optimization steps:

1. Schema evolution: normalize old and new schemas by adding missing evolved columns with defaults.
2. Duplicate handling: keep the latest record per `event_id` using `ingestion_ts`.
3. Skew handling: detect hot product keys and salt them into multiple buckets before aggregation.
4. High cardinality handling: keep top campaign ids and hash rare campaigns into bounded buckets.
5. Shuffle/work reduction: replace repeated scans with one-pass pre-aggregation.

Optimized result from `reports/processing_jobs/spark_optimized.json`:

| Metric | Value |
| --- | ---: |
| Input rows | 12,360 |
| Rows used | 12,000 |
| Schema evolution rows dropped | 0 |
| Schema defaults applied | 5,563 |
| Duplicates removed | 360 |
| Bounded campaign cardinality | 96 |
| Max partition ratio | 1.2967 |
| Operation count | 12,000 |
| Duration | 75.120 ms |

Before/after comparison from `reports/processing_jobs/spark_comparison.json`:

| Metric | Value |
| --- | ---: |
| Duration speedup | 9.229x |
| Operation reduction | 1115.274x |
| Partition ratio improvement | 3.891x |
| Schema rows recovered | 5,563 |
| Duplicates removed after optimize | 360 |
| Campaign cardinality reduction | 2,215 |

### Spark UI Screenshot Steps

Run the real Airflow Spark batch pipeline:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops
make cluster-data-setup
```

Open Airflow:

```bash
kubectl port-forward -n recsys-dataflow svc/airflow-webserver 8080:8080
```

Screenshot proof to capture:

- Airflow Graph view showing `ingest_historical_batch_to_lakehouse -> run_spark_batch_to_offline_store -> feast_materialize_incremental`.
- Airflow task logs for `run_spark_batch_to_offline_store`.

Open Spark UI while the Spark driver pod is running:

```bash
kubectl get pods -n recsys-dataflow | rg 'spark|driver'
kubectl port-forward -n recsys-dataflow pod/<spark-driver-pod> 4040:4040
```

Screenshot proof to capture in Spark UI:

- Jobs tab: baseline run has longer duration.
- Stages tab: baseline has skew symptoms such as one task much longer than the median task.
- SQL tab: baseline has larger shuffle/read pressure; optimized run should show lower runtime and less severe skew.
- Executors tab: compare task time distribution and spill/shuffle metrics.

## Flink Streaming Job

### Baseline

Baseline behavior:

- Processes every event without event-id deduplication.
- Does not detect late arrivals.
- Does not emit quality windows.
- Does not mark bursty windows.
- Keeps unbounded event history while computing state.

Baseline result from `reports/processing_jobs/flink_baseline.json`:

| Metric | Value |
| --- | ---: |
| Input events | 5,200 |
| Events processed | 5,200 |
| Duplicate events written | 200 |
| Late events detected | 0 |
| Windows emitted | 0 |
| Bursty windows | 0 |
| Operation count | 13,517,400 |
| Duration | 1044.685 ms |

### Optimized

Optimization steps:

1. Duplicate handling: skip repeated `event_id` values with TTL-bounded state.
2. Late arrival handling: compare event time and processed time using a watermark delay.
3. Burst handling: aggregate event counts into fixed event-time windows and flag windows over threshold.
4. Window processing: emit `streaming_quality_windows` with `window_start`, `window_end`, `event_count`, `late_event_count`, `duplicate_event_count`, and `is_bursty`.
5. State optimization: keep keyed user state bounded with TTL.
6. Reliability: enable checkpointing in the production PyFlink job.

Optimized result from `reports/processing_jobs/flink_optimized.json`:

| Metric | Value |
| --- | ---: |
| Input events | 5,200 |
| Events processed | 5,000 |
| Duplicate events skipped | 200 |
| Duplicate events written | 0 |
| Late events detected | 737 |
| Windows emitted | 85 |
| Bursty windows | 1 |
| Operation count | 5,000 |
| Duration | 616.431 ms |

Before/after comparison from `reports/processing_jobs/flink_comparison.json`:

| Metric | Value |
| --- | ---: |
| Duration speedup | 1.695x |
| Operation reduction | 2703.480x |
| Duplicates no longer written | 200 |
| Late events detected after optimize | 737 |
| Windows emitted after optimize | 85 |
| Bursty windows after optimize | 1 |

### Window Processing Code To Capture

Capture this code section as proof:

- [apps/data-platform/src/features/flink/realtime_stream_job.py line 637](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#637): `StreamingQualityRows`.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 658](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#658): computes late arrival metrics per event.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 662](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#662): computes the fixed event-time window start.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 681](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#681): updates late event count.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 816](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#816): writes `streaming_quality_windows`.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 840](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#840): enables checkpointing.

### Flink UI Screenshot Steps

Start or verify the streaming feature job:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops
make cluster-data-setup

kubectl exec -n recsys-dataflow deploy/flink-jobmanager -- \
  curl -fsS http://localhost:8081/jobs/overview
```

Open Flink UI:

```bash
kubectl port-forward -n recsys-dataflow svc/flink-jobmanager 8082:8081
```

Open `http://127.0.0.1:8082`.

Screenshot proof to capture:

- Jobs overview: streaming job is `RUNNING`.
- Job graph: source -> parse/normalize -> dedup -> user/item feature state -> Redis/Iceberg sinks.
- Operators tab: `redis-online-feature-writer` and Iceberg sink operators are present.
- Checkpoints tab: checkpointing is enabled and checkpoints complete.
- Backpressure tab: no sustained high backpressure after optimization.
- Exceptions tab: no active failures.

CLI proof for streaming output:

```bash
make data-platform-verify-e2e

kubectl exec -n recsys-dataflow deploy/flink-jobmanager -- \
  curl -fsS http://localhost:8081/jobs/overview
```

The verification checks Iceberg offline feature tables, Redis online feature keys, and the running Flink job.

