# Local Dataflow Docker Commands

File này ghi nhanh các command để build, bật, tắt, kiểm tra local dataflow stack.

Stack gồm: MinIO, Postgres, Kafka, Kafka Connect/Debezium, Flink, Redis, Feast/dataflow-cli, Airflow, Spark.

## Start Services

Build images rồi bật toàn bộ services:

```bash
make dataflow-up-build
```

Chỉ bật services, không rebuild image:

```bash
make dataflow-up
```

## Stop Services

Tắt và remove containers, giữ lại Docker volumes:

```bash
make dataflow-down
```

Dùng command này khi muốn dừng stack nhưng vẫn giữ data trong Postgres, MinIO, Redis, Kafka, Airflow DB.

Tắt stack và xóa luôn Docker volumes:

```bash
make dataflow-down-volumes
```

Dùng command này khi muốn reset sạch local state. Sau lệnh này, MinIO buckets/data, Postgres data, Redis data, Kafka data, Airflow metadata sẽ mất.

## Restart Services

Restart stack, giữ volumes:

```bash
make dataflow-restart
```

## Check Status

Xem container nào đang chạy, port nào đang expose:

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
make dataflow-logs DATAFLOW_LOG_SERVICE=spark-master
make dataflow-logs DATAFLOW_LOG_SERVICE=kafka-connect
```

## Smoke Checks

Kiểm tra toàn bộ stack, buckets, connectors, bronze CDC, offline feature outputs, và Redis online keys:

```bash
make dataflow-smoke
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

## Trigger Airflow DAG

## Run One Full E2E Flow

Trigger DAG chính một lần:

```bash
make dataflow-e2e
```

Command này chạy DAG `full_dataflow_local_dag`, tức là một E2E batch/realtime verification run. Nó không bật realtime loop vô hạn.

Trigger DAG trực tiếp nếu chỉ muốn gọi Airflow:

```bash
make dataflow-trigger
```

Mặc định DAG là:

```text
full_dataflow_local_dag
```

DAG này chạy đủ 2 nhánh:

```text
historical_bootstrap_path:
  generator -> recsys-lake/raw -> Spark batch -> recsys-feature-store/offline -> Feast -> Redis

realtime path:
  generator -> Postgres -> Debezium -> Kafka
    -> Kafka S3 sink -> recsys-lake/bronze -> Spark batch -> recsys-feature-store/offline -> Feast -> Redis
    -> bounded PyFlink realtime job -> Redis
```

Trigger DAG khác nếu cần:

```bash
make dataflow-trigger DATAFLOW_DAG=full_dataflow_local_dag
```

## Ingest Historical Data Into Data Lake

Generate historical data rồi ghi vào MinIO lake bucket:

```bash
make dataflow-ingest-lake
```

Default path:

```text
s3://recsys-lake/raw/<run_id>/<table_name>/...
```

Override bucket/prefix nếu cần:

```bash
make dataflow-ingest-lake DATAFLOW_INGEST_BUCKET=recsys-lake DATAFLOW_INGEST_PREFIX=raw
```

## Continuous Realtime Flow

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

## Run Tests

Chạy unit tests local:

```bash
make dataflow-test
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

## Notes

`make dataflow-down` là command nên dùng thường ngày vì nó không xóa data.

`make dataflow-down-volumes` chỉ dùng khi muốn reset sạch môi trường local.

Nếu sửa Dockerfile hoặc dependencies, chạy:

```bash
make dataflow-up-build
```

Nếu chỉ sửa code Python được copy vào image, cũng nên rebuild image tương ứng bằng:

```bash
make dataflow-build
make dataflow-up
```
