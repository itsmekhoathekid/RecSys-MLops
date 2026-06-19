# Local Dataflow Make Commands

File này gom toàn bộ `make` commands hiện có cho local dataflow stack.

Stack gồm: MinIO, Postgres, Kafka, Kafka Connect/Debezium, Flink, Redis, Feast/dataflow-cli, Airflow, Spark.

## Quick Reference

```bash
make help
make dataflow-build
make dataflow-up
make dataflow-up-build
make dataflow-down
make dataflow-down-volumes
make dataflow-restart
make dataflow-ps
make dataflow-logs
make dataflow-smoke
make dataflow-trigger
make dataflow-e2e
make dataflow-ingest-lake
make dataflow-realtime-up
make dataflow-realtime-down
make dataflow-test
```

## Help

In toàn bộ command có trong Makefile:

```bash
make help
```

## Build Images

Build các Docker images cho dataflow stack:

```bash
make dataflow-build
```

Dùng command này khi sửa Dockerfile, dependency, hoặc code được copy vào image.

## Start Services

Bật toàn bộ services, không rebuild image:

```bash
make dataflow-up
```

Build images rồi bật toàn bộ services:

```bash
make dataflow-up-build
```

Dùng command này cho lần chạy đầu tiên hoặc sau khi sửa Dockerfile/dependencies.

## Stop Services

Tắt và remove containers, giữ lại Docker volumes:

```bash
make dataflow-down
```

Command này giữ data trong Postgres, MinIO, Redis, Kafka, Airflow metadata.

Tắt stack và xóa luôn Docker volumes:

```bash
make dataflow-down-volumes
```

Command này reset sạch local state. MinIO buckets/data, Postgres data, Redis data, Kafka data, Airflow metadata sẽ mất.

## Restart Services

Restart stack, giữ volumes:

```bash
make dataflow-restart
```

Tương đương:

```bash
make dataflow-down
make dataflow-up
```

## Service Status

Xem containers, status, và ports:

```bash
make dataflow-ps
```

## Logs

Tail logs toàn bộ stack:

```bash
make dataflow-logs
```

Tail logs một service cụ thể:

```bash
make dataflow-logs DATAFLOW_LOG_SERVICE=airflow-webserver
make dataflow-logs DATAFLOW_LOG_SERVICE=airflow-scheduler
make dataflow-logs DATAFLOW_LOG_SERVICE=kafka-connect
make dataflow-logs DATAFLOW_LOG_SERVICE=spark-master
make dataflow-logs DATAFLOW_LOG_SERVICE=flink-jobmanager
make dataflow-logs DATAFLOW_LOG_SERVICE=redis
make dataflow-logs DATAFLOW_LOG_SERVICE=minio
```

## Smoke Checks

Kiểm tra toàn bộ stack, buckets, connectors, bronze CDC, offline feature outputs, và Redis online keys:

```bash
make dataflow-smoke
```

Mặc định:

```text
DATAFLOW_SMOKE_PHASE=all
```

Chạy từng phase:

```bash
make dataflow-smoke DATAFLOW_SMOKE_PHASE=services
make dataflow-smoke DATAFLOW_SMOKE_PHASE=buckets
make dataflow-smoke DATAFLOW_SMOKE_PHASE=connectors
make dataflow-smoke DATAFLOW_SMOKE_PHASE=bronze
make dataflow-smoke DATAFLOW_SMOKE_PHASE=offline
make dataflow-smoke DATAFLOW_SMOKE_PHASE=redis
make dataflow-smoke DATAFLOW_SMOKE_PHASE=all
```

Ý nghĩa nhanh:

```text
services   kiểm tra MinIO, Kafka Connect, Redis reachable
buckets    kiểm tra đúng 2 buckets: recsys-lake, recsys-feature-store
connectors kiểm tra Debezium + Kafka MinIO sink đang RUNNING
bronze     kiểm tra CDC objects trong recsys-lake/bronze
offline    kiểm tra Feast offline feature outputs
redis      kiểm tra Redis online feature keys
all        chạy tất cả checks trên
```

## Trigger Airflow DAG

Trigger DAG mặc định:

```bash
make dataflow-trigger
```

Mặc định:

```text
DATAFLOW_DAG=full_dataflow_local_dag
```

Trigger DAG khác nếu cần:

```bash
make dataflow-trigger DATAFLOW_DAG=full_dataflow_local_dag
```

Command này chỉ trigger DAG. Nó không tự bật realtime continuous loop.

## Run One Full E2E Flow

Trigger một E2E DAG run:

```bash
make dataflow-e2e
```

Command này chạy `full_dataflow_local_dag` một lần. Nó verify đủ historical path và realtime path, nhưng không bật realtime loop vô hạn.

Flow trong DAG:

```text
historical_bootstrap_path:
  generator -> recsys-lake/raw -> Spark batch -> recsys-feature-store/offline -> Feast -> Redis

realtime path:
  generator -> Postgres -> Debezium -> Kafka
    -> Kafka S3 sink -> recsys-lake/bronze -> Spark batch -> recsys-feature-store/offline -> Feast -> Redis
    -> bounded realtime stream job -> Redis
```

Có thể override DAG hoặc smoke phase truyền vào script:

```bash
make dataflow-e2e DATAFLOW_DAG=full_dataflow_local_dag
make dataflow-e2e DATAFLOW_SMOKE_PHASE=all
```

## Ingest Historical Data Into Data Lake

Generate historical data rồi ghi vào MinIO lake bucket:

```bash
make dataflow-ingest-lake
```

Default:

```text
DATAFLOW_INGEST_BUCKET=recsys-lake
DATAFLOW_INGEST_PREFIX=raw
```

Default output path:

```text
s3://recsys-lake/raw/<run_id>/<table_name>/...
```

Override bucket/prefix nếu cần:

```bash
make dataflow-ingest-lake DATAFLOW_INGEST_BUCKET=recsys-lake DATAFLOW_INGEST_PREFIX=raw
```

## Start Continuous Realtime Flow

Bật realtime continuous mode:

```bash
make dataflow-realtime-up
```

Command này sẽ:

```text
init Postgres schema
register Debezium connector
register Kafka -> MinIO bronze sink
start continuous Postgres producer container
start continuous Flink-runtime Kafka consumer container
```

Realtime flow khi bật:

```text
Data Generator loop -> Postgres source system
  -> Debezium CDC -> Kafka
    -> Kafka S3 sink -> recsys-lake/bronze
    -> realtime stream job -> Redis online store
```

Sau khi bật có thể check:

```bash
make dataflow-smoke DATAFLOW_SMOKE_PHASE=connectors
make dataflow-smoke DATAFLOW_SMOKE_PHASE=bronze
make dataflow-smoke DATAFLOW_SMOKE_PHASE=redis
```

## Stop Continuous Realtime Flow

Tắt realtime continuous mode:

```bash
make dataflow-realtime-down
```

Command này chỉ stop/remove 2 container loop:

```text
recsys-dataflow-realtime-producer
recsys-dataflow-realtime-flink
```

Nó không tắt toàn bộ stack và không xóa Docker volumes.

## Tests

Chạy unit tests local:

```bash
make dataflow-test
```

Hiện command này chạy:

```bash
uv run pytest data_generator/tests testing/unit -q
```

## Local UIs

Airflow:

```text
http://localhost:8088
username: admin
password: admin
```

MinIO:

```text
http://localhost:9001
username: minio
password: minio123
```

Spark UI:

```text
http://localhost:8080
```

Flink UI:

```text
http://localhost:8082
```

Kafka Connect:

```text
http://localhost:8083/connectors
```

Schema Registry:

```text
http://localhost:8081/subjects
```

## Recommended Workflows

Lần đầu chạy stack:

```bash
make dataflow-up-build
make dataflow-smoke DATAFLOW_SMOKE_PHASE=services
make dataflow-e2e
make dataflow-smoke DATAFLOW_SMOKE_PHASE=all
```

Chỉ test historical ingest:

```bash
make dataflow-up
make dataflow-ingest-lake
make dataflow-smoke DATAFLOW_SMOKE_PHASE=buckets
```

Chạy realtime liên tục:

```bash
make dataflow-up
make dataflow-realtime-up
make dataflow-smoke DATAFLOW_SMOKE_PHASE=connectors
make dataflow-smoke DATAFLOW_SMOKE_PHASE=bronze
make dataflow-smoke DATAFLOW_SMOKE_PHASE=redis
```

Tắt realtime loop nhưng giữ platform:

```bash
make dataflow-realtime-down
```

Tắt toàn bộ platform nhưng giữ data:

```bash
make dataflow-down
```

Reset sạch local environment:

```bash
make dataflow-down-volumes
make dataflow-up-build
```

## Notes

`make dataflow-e2e` và `make dataflow-trigger` là one-shot Airflow DAG run.

`make dataflow-realtime-up` mới là command bật realtime flow chạy liên tục.

`make dataflow-down` là command nên dùng thường ngày vì nó không xóa data.

`make dataflow-down-volumes` chỉ dùng khi muốn reset sạch môi trường local.
