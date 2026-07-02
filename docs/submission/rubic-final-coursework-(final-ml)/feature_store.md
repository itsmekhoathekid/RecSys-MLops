# Feature Store

## Feast Store Definition

This project defines the Feast store split as:

| Feast layer | Backing system | Purpose |
| --- | --- | --- |
| Offline store | Apache Iceberg tables in the lakehouse warehouse, exported to Feast FileSource views under `s3://recsys-offline-feature-store/feast/offline` | Historical feature storage for batch training, validation, drift checks, and Feast historical retrieval/materialization jobs. |
| Online store | Redis in namespace `recsys-dataflow` | Low-latency feature lookup for serving APIs by `user_id` and `item_id`. |

MinIO is the S3-compatible storage backend for the Iceberg warehouse. The authoritative offline store is Apache Iceberg. Feast 0.64 does not ship a native Iceberg offline-store provider, so Spark writes the Iceberg feature tables and also exports Feast-compatible Parquet views. Kubeflow then uses Feast `get_historical_features` over those exported views instead of bypassing Feast and reading the merged training table directly. Redis is the online store used by the serving APIs.

The serving split uses this contract:

```text
Spark/Flink -> Apache Iceberg offline store -> Feast FileSource views
Kubeflow prepare-training-data -> Feast get_historical_features -> BST dataset splits
Feast materialize / Flink online-store job -> Redis online store
FastAPI online feature API -> Redis online store
FastAPI recommendation API -> online feature API -> Triton inference
```

## Data Pipeline And Incremental Materialize

Code reference:

- [apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py line 185](../../../apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py#L185): generates historical raw files into the lake bucket.
- [apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py line 194](../../../apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py#L194): ingests historical raw data into the lakehouse.
- [apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py line 215](../../../apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py#L215): runs Spark batch materialization into the offline feature store.
- [apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py line 232](../../../apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py#L232): runs Feast `materialize-incremental` after offline feature tables are written.
- [apps/data-platform/feature-store/feature_repo/feature_store.yaml line 1](../../../apps/data-platform/feature-store/feature_repo/feature_store.yaml#L1): Feast project, offline store, and Redis online store configuration.
- [apps/data-platform/feature-store/feature_repo/features.py line 20](../../../apps/data-platform/feature-store/feature_repo/features.py#L20): Feast `user_sequence_features` FileSource exported from the Iceberg offline store.
- [apps/data-platform/feature-store/feature_repo/features.py line 37](../../../apps/data-platform/feature-store/feature_repo/features.py#L37): Feast `user_sequence_features` FeatureView.
- [apps/data-platform/feature-store/feature_repo/features.py line 63](../../../apps/data-platform/feature-store/feature_repo/features.py#L63): Feast `user_aggregate_features` FeatureView.
- [apps/data-platform/feature-store/feature_repo/features.py line 83](../../../apps/data-platform/feature-store/feature_repo/features.py#L83): Feast `item_features` FeatureView.
- [apps/data-platform/feature-store/feature_repo/features.py line 106](../../../apps/data-platform/feature-store/feature_repo/features.py#L106): Feast FeatureService `bst_ranking_v1` used by Kubeflow training preparation.
- [apps/data-platform/src/features/spark/spark_batch_entrypoint.py line 111](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L111): exports `user_sequence_features`, `user_aggregate_features`, and `item_features` from Iceberg outputs into Feast offline views.
- [apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py line 291](../../../apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py#L291): defines `run_spark_batch_to_offline_store -> feast_materialize_incremental`.

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

make cluster-data-setup

# Keep this terminal open for Airflow UI access.
kubectl port-forward -n recsys-dataflow svc/airflow-webserver 8080:8080
```

Open Airflow UI at `http://localhost:8080`, login with `admin/admin`

Description of output when running command:

- `make cluster-data-setup` starts the data platform stack, triggers `k8s_data_platform_dag`, waits for the DAG run, and verifies feature stores.
- Airflow Graph view demonstrates the pipeline stage order: platform initialization, historical batch ingestion, Spark offline feature materialization, Feast incremental materialization, realtime CDC load, Flink streaming feature-store sync, drift check, retrain trigger, and governance ingest.
- The important proof for this rubric is `run_spark_batch_to_offline_store -> feast_materialize_incremental`: Spark writes the latest feature tables to the offline store, then Feast runs `feast apply` and `feast materialize-incremental <end_datetime>`.
- This follows the Feast incremental materialize pattern: Feast uses the registry materialization state to infer the start time and only loads feature rows up to the requested end time into the Redis online store.

### Image proof of Airflow data pipelines overview

![Data & ML system](../../pngs/airflow_data_pipeline_overview.png)

### Image proof of Incremental materialize data from offline to online store

![Data & ML system](../../pngs/feast_materialize_dag.png)

## Two Running Streaming Feature Store Jobs

Code reference:

- [infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml](../../../infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml): deploys two continuous Flink submitters, one for Redis online features and one for Iceberg offline features.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 483](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L483): Redis online feature writer.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 735](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L735): names the online sink `redis-online-feature-writer`.
- [apps/data-platform/src/features/flink/realtime_stream_job.py](../../../apps/data-platform/src/features/flink/realtime_stream_job.py): `--disable-offline-store` runs the Redis-only job; `--offline-store-enabled --disable-online-store` runs the Iceberg-only job.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 779](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L779): writes streaming behavior events to the Iceberg offline feature store.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 781](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L781): writes streaming user sequence features to the Iceberg offline feature store.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 791](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L791): writes streaming user aggregate features to the Iceberg offline feature store.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 801](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L801): writes streaming item features to the Iceberg offline feature store.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 817](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L817): writes streaming quality windows to the Iceberg offline feature store.

Running command:

```bash
cd RecSys-MLops

# Terminal 1: keep open for Flink UI.
kubectl port-forward -n recsys-dataflow svc/flink-jobmanager 8081:8081

# Terminal 2: show the deployed streaming feature job and its Flink job id.
kubectl get deploy -n recsys-dataflow \
  realtime-flink-online-store \
  realtime-flink-offline-store

kubectl exec -n recsys-dataflow deploy/flink-jobmanager -- \
  curl -fsS http://localhost:8081/jobs/overview
```

Open Flink UI at `http://localhost:8081/#/job/running/<job_id>/overview`, using the `jid` from `jobs/overview`.

Description of output when running command:

- This proof shows two continuous Flink jobs listening to the same Kafka CDC topic with different consumer groups.
- `realtime-flink-online-store` writes Redis keys such as `fs:user_sequence:*`, `fs:user_aggregate:*`, `fs:item:*`, and candidate sorted sets for low-latency API serving.
- `realtime-flink-offline-store` writes Iceberg tables: `stream_behavior_events`, `stream_user_sequence_features`, `stream_user_aggregate_features`, `stream_item_features`, and `streaming_quality_windows`.
- The Flink UI and `jobs/overview` output should show both jobs in `RUNNING` state.

### Image proof of CLI running job

![Data & ML system](../../pngs/two_streaming_job_cli.png)

### Image proof of one streaming job with two sink paths

![Data & ML system](../../pngs/flink_ui_streaming_jobs.png)

### Image proof of Flink operator names

![Data & ML system](../../pngs/flink_ui_streaming_job_operators.png)

### Flink UI Name descriptions

| Name in Flink UI | Description |
| --- | --- |
| `Source: cdc-behavior-events-source -> Map, Filter, _stream_key_by_map_operator` | Reads CDC behavior events from Kafka, parses and normalizes the payload, filters invalid records, and keys the stream for downstream feature processing. |
| `KEYED PROCESS -> (_stream_key_by_map_operator, _stream_key_by_map_operator)` | Deduplicates events by `event_id` and fans out the validated stream into feature-building and quality-monitoring branches. |
| `KEYED PROCESS, _stream_key_by_map_operator` | Builds keyed user-level features from the deduplicated behavior stream, such as sequence and aggregate feature inputs. |
| `KEYED PROCESS -> redis-online-feature-writer -> ... IcebergStreamWriter` | Builds item-level features, writes fresh online features through the Redis writer path, and converts records into Iceberg table rows for offline storage. |
| `KEYED PROCESS, redis-online-feature-writer` | Online feature-store writer path; writes fresh streaming feature values to Redis for low-latency serving. |
| `IcebergFilesCommitter -> Sink: IcebergSink ...stream_behavior_events` | Offline feature-store sink for cleaned streaming behavior events. |
| `IcebergFilesCommitter -> Sink: IcebergSink ...stream_user_sequence_features` | Offline feature-store sink for user sequence features, including recent interaction history payloads. |
| `IcebergFilesCommitter -> Sink: IcebergSink ...stream_user_aggregate_features` | Offline feature-store sink for user rolling aggregate counters such as recent views, carts, and purchases. |
| `IcebergFilesCommitter -> Sink: IcebergSink ...stream_item_features` | Offline feature-store sink for item popularity and item-level rolling features. |
| `KEYED PROCESS, Map -> *anonymous_datastream_source$5* -> Calc -> IcebergStreamWriter` | Builds streaming quality-window rows, including event count, late-event count, duplicate count, max lateness, and burst flag. |
| `IcebergFilesCommitter -> Sink: IcebergSink ...streaming_quality_windows` | Offline feature-store sink for streaming quality monitoring windows. |



## Streaming Features Pushed To Offline Store

Code reference:

- [apps/data-platform/src/features/flink/iceberg_feature_sink.py line 8](../../../apps/data-platform/src/features/flink/iceberg_feature_sink.py#L8): defines Iceberg offline streaming feature table DDLs.
- [apps/data-platform/src/features/flink/iceberg_feature_sink.py line 80](../../../apps/data-platform/src/features/flink/iceberg_feature_sink.py#L80): configures the Iceberg feature catalog and creates feature tables.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 781](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L781): writes `stream_user_sequence_features`.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 791](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L791): writes `stream_user_aggregate_features`.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 801](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L801): writes `stream_item_features`.
- [infra/k8s/scripts/data_platform_verify_feature_stores.sh line 152](../../../infra/k8s/scripts/data_platform_verify_feature_stores.sh#L152): verifies offline feature table metadata and data files.

Running command:

```bash
cd RecSys-MLops

make data-platform-verify-e2e
```

Description of output when running command:

- The verification job checks that the offline Iceberg feature store contains metadata files and Parquet data files.
- The checked offline streaming tables are `stream_behavior_events`, `stream_user_sequence_features`, `stream_user_aggregate_features`, `stream_item_features`, and `streaming_quality_windows`.
- The JSON output should show each table with `metadata_files > 0` and `data_files > 0`.

### Image proof 

![Data & ML system](../../pngs/streaming_feats_to_offline.png)


## Streaming Features Pushed To Online Store

Code reference:

- [apps/data-platform/src/feature_store/online_writer.py line 30](../../../apps/data-platform/src/feature_store/online_writer.py#L30): Redis online writer used by the streaming job.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 483](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L483): Redis writer operator class.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 498](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L498): writes user sequence, user aggregate, and item payloads to Redis.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 735](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L735): names the Redis online sink `redis-online-feature-writer`.
- [infra/k8s/scripts/data_platform_verify_feature_stores.sh line 76](../../../infra/k8s/scripts/data_platform_verify_feature_stores.sh#L76): waits until Redis online feature keys exist.
- [infra/k8s/scripts/data_platform_verify_feature_stores.sh line 180](../../../infra/k8s/scripts/data_platform_verify_feature_stores.sh#L180): prints Redis online feature key verification.

Running command:

```bash
cd RecSys-MLops

kubectl exec -n recsys-dataflow deploy/redis -- \
  sh -lc 'redis-cli --scan --pattern "fs:user_sequence:*" | head'

kubectl exec -n recsys-dataflow deploy/redis -- \
  sh -lc 'redis-cli --scan --pattern "fs:user_aggregate:*" | head'

kubectl exec -n recsys-dataflow deploy/redis -- \
  sh -lc 'redis-cli --scan --pattern "fs:item:*" | head'

make data-platform-verify-e2e
```

Description of output when running command:

- The Redis commands show online feature keys created by the streaming Flink job.
- `fs:user_sequence:*` proves streaming user sequence features were pushed into the online store.
- `fs:user_aggregate:*` proves streaming user aggregate features were pushed into the online store.
- `fs:item:*` proves streaming item features were pushed into the online store.
- `make data-platform-verify-e2e` prints `Redis online feature keys detected: ...` and `Streaming feature stores verified.` when the online feature store proof passes.

### Image proof 

![Data & ML system](../../pngs/streaming_feats_to_online.png)

## Feature Columns And TTL

Code reference:

- [apps/data-platform/src/features/spark/build_user_sequence_features.py line 6](../../../apps/data-platform/src/features/spark/build_user_sequence_features.py#L6): defines user sequence feature columns.
- [apps/data-platform/src/features/spark/build_user_aggregate_features.py line 26](../../../apps/data-platform/src/features/spark/build_user_aggregate_features.py#L26): defines user aggregate feature columns.
- [apps/data-platform/src/features/spark/build_item_features.py line 49](../../../apps/data-platform/src/features/spark/build_item_features.py#L49): defines item feature columns.
- [apps/data-platform/src/features/spark/spark_batch_entrypoint.py line 65](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L65): writes batch feature tables to the offline feature store.
- [apps/data-platform/src/features/spark/spark_batch_entrypoint.py line 109](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L109): writes Feast parquet mirror for offline-to-online materialization.
- [apps/data-platform/feature-store/feature_repo/features.py line 36](../../../apps/data-platform/feature-store/feature_repo/features.py#L36): Feast TTL for `user_aggregate_features`.
- [apps/data-platform/feature-store/feature_repo/features.py line 55](../../../apps/data-platform/feature-store/feature_repo/features.py#L55): Feast TTL for `item_features`.
- [configs/local/redis_online_store.yaml line 14](../../../configs/local/redis_online_store.yaml#L14): documents native Redis TTL values for streaming online writer.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 871](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L871): default Flink state TTL.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 872](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L872): default Flink dedup state TTL.

Running command:

```bash
cd RecSys-MLops

kubectl exec -n recsys-dataflow deploy/redis -- \
  sh -lc 'key=$(redis-cli --scan --pattern "fs:user_sequence:*" | head -1); echo "$key"; redis-cli ttl "$key"'

kubectl exec -n recsys-dataflow deploy/redis -- \
  sh -lc 'key=$(redis-cli --scan --pattern "fs:user_aggregate:*" | head -1); echo "$key"; redis-cli ttl "$key"'

kubectl exec -n recsys-dataflow deploy/redis -- \
  sh -lc 'key=$(redis-cli --scan --pattern "fs:item:*" | head -1); echo "$key"; redis-cli ttl "$key"'
```

Description of output when running command:

- Each Redis command prints one matching online feature key, followed by the remaining TTL in seconds for that key.
- `fs:user_sequence:*` should return a TTL close to `7,776,000` seconds for a freshly written sequence feature.
- `fs:user_aggregate:*` should return a TTL close to `86,400` seconds for a freshly written user aggregate feature.
- `fs:item:*` should return a TTL close to `604,800` seconds for a freshly written item feature.
- A positive TTL proves the feature is stored in Redis with expiry enabled. A result of `-2` means the key does not exist, and `-1` means the key exists but no expiry was set.

Feature columns and TTL explanation:

| Feature group | Main columns | Storage path | TTL | Why this TTL |
| --- | --- | --- | --- | --- |
| `user_sequence` | `hist_item_ids`, `hist_event_type_ids`, `hist_category_ids`, `hist_brand_ids`, `hist_price_bucket_ids`, `hist_event_timestamps`, `hist_request_ids`, `hist_impression_ids` | Native Redis online store key `fs:user_sequence:{user_id}` | `7,776,000` seconds (`90 days`) | Sequence features represent longer user history for sequential recommendation models. A longer TTL keeps enough interaction context for ranking while still allowing inactive users to age out. |
| `user_aggregate_features` / `user_aggregate` | `views_30m`, `carts_30m`, `purchases_24h`, `distinct_categories_7d`, `avg_viewed_price_7d`, `cart_to_purchase_ratio_7d`, `last_event_age_seconds`, `feature_version` | Feast FeatureView for batch materialization, plus Redis key `fs:user_aggregate:{user_id}` for streaming online serving | Feast TTL `1 day`; Redis TTL `86,400` seconds (`1 day`) | User aggregate features describe recent intent, so stale values can mislead recommendations quickly. The short TTL forces fresh behavior counts and prevents old intent from being served. |
| `item_features` / `item` | `category_id`, `brand_id`, `price_bucket`, `is_active`, `views_1h`, `views_24h`, `carts_1h`, `carts_24h`, `purchases_24h`, `purchases_7d`, `conversion_rate_7d`, `popularity_score`, `feature_version` | Feast FeatureView for batch materialization, plus Redis key `fs:item:{product_id}` for streaming online serving | Feast TTL `7 days`; Redis TTL `604,800` seconds (`7 days`) | Item metadata and popularity are more stable than user intent, but popularity still changes over time. A weekly TTL keeps item features useful without serving very old trends. |
| Flink keyed state | Per-user history, per-item history, dedup ids, and streaming quality windows used while computing features | Flink internal state | Feature state TTL `7 days`; dedup state TTL `1 day` | State TTL bounds memory/storage used by the streaming job. Dedup ids only need a short retention window, while feature-building state needs enough history for rolling and sequence features. |

### Image proof 

![Data & ML system](../../pngs/TTL.png)
