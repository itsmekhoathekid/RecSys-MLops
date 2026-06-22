# Unused / Not-Currently-Used Code Audit

Scope cua audit nay:

- "Current KFP/Ray E2E" = flow vua deploy/run: feature engineering container -> prepare JSONL -> KubeRay RayJob tune/train -> evaluate -> MLflow/MinIO/Postgres.
- "Unused trong KFP/Ray E2E" khong co nghia la file do vo dung; co file van duoc docker-compose/Airflow/realtime/Feast path dung.
- "Candidate prune" la file khong co runtime reference ro rang, hoac chi la scaffold/test-only.

## Current KFP/Ray E2E Active Files

### Data Platform Active In KFP/Ray E2E

`apps/ml-system/src/run_feature_engineering.py` : KFP/direct smoke entrypoint; goi `recsys_data_platform.local.run_batch_features.run_batch_features`.

`apps/data-platform/src/local/run_batch_features.py` : active core runner; orchestrate silver tables, user/item features, ranking labels va BST training table.

`apps/data-platform/src/feature_engineering/spark/build_silver_tables.py` : active; doc raw generator run, clean silver tables.

`apps/data-platform/src/feature_engineering/spark/build_user_sequence_features.py` : active; tao sequence features cho BST.

`apps/data-platform/src/feature_engineering/spark/build_user_aggregate_features.py` : active; tao user aggregate features.

`apps/data-platform/src/feature_engineering/spark/build_item_features.py` : active; tao item features.

`apps/data-platform/src/feature_engineering/spark/build_ranking_labels.py` : active; tao ranking labels.

`apps/data-platform/src/feature_engineering/spark/build_bst_training_table.py` : active; join labels + features thanh `ml_bst_training`.

`apps/data-platform/src/ingest/minio_raw_reader.py` : active indirectly; `build_silver_tables.py` dung `read_generator_run`.

`apps/data-platform/src/preprocess/event_dedup.py` : active indirectly; `build_silver_tables.py` dung dedup behavior events.

`apps/data-platform/src/preprocess/schema_evolution.py` : active indirectly; `build_silver_tables.py` dung normalize schemas.

`apps/data-platform/src/preprocess/point_in_time.py` : active indirectly; `build_bst_training_table.py` dung point-in-time/time bucket logic.

`apps/data-platform/src/feature_store/offline_writer.py` : active; write offline parquet outputs va read `ml_bst_training` trong `prepare_bst_training_data.py`.

### Model Pipeline Active In KFP/Ray E2E

`apps/ml-system/src/prepare_bst_training_data.py` : active KFP step; convert `ml_bst_training` offline table sang `train.jsonl`, `val.jsonl`, `test.jsonl`.

`apps/ml-system/src/submit_ray_job.py` : active KFP step; submit/wait KubeRay `RayJob`.

`apps/ml-system/src/ray_tune_train_bst.py` : active RayJob entrypoint; Ray Tune trials + best result registry.

`apps/ml-system/src/evaluate_ray_best_bst.py` : active KFP step; doc `best_result.json`, evaluate checkpoint tren test split.

`apps/ml-system/src/evaluate_bst.py` : active indirectly; wrapper eval goi file nay.

`apps/ml-system/src/model_registry.py` : active indirectly; `apps/ml-system/src/train.py` va Ray best registration ghi config vao Postgres.

`apps/ml-system/src/train.py` : active; Ray trials goi `run_training`.

`apps/ml-system/src/models/dataset.py` : active; training/eval dataset loader.

`apps/ml-system/src/models/model.py` : active; BST model architecture.

`apps/ml-system/src/models/trainer.py` : active; train/eval/checkpoint logic.

## Model Pipeline Unused Status

Ket luan: hien tai **khong thay file code dead ro rang trong `apps/ml-system/src`**.

`apps/ml-system/src/__init__.py` : package marker, khong co business logic; giu lai.

`apps/ml-system/src/evaluate_bst.py` : khong duoc KFP goi truc tiep, nhung duoc `evaluate_ray_best_bst.py` import; giu lai.

`apps/ml-system/src/model_registry.py` : khong phai KFP step rieng, nhung duoc `apps/ml-system/src/train.py` va `ray_tune_train_bst.py` dung; giu lai.

## Data Platform Files Not Used In Current KFP/Ray E2E But Used Elsewhere

Nhom nay khong nen xoa neu van muon giu docker-compose/Airflow/realtime/Feast flow.

`apps/data-platform/src/feature_engineering/spark/spark_batch_entrypoint.py` : khong dung trong KFP flow; duoc Airflow `full_dataflow_local_dag.py` dung qua `spark-submit`.

`apps/data-platform/src/feature_engineering/spark/spark_realtime_bronze_entrypoint.py` : khong dung trong KFP flow; duoc Airflow full dataflow realtime batch path dung.

`apps/data-platform/src/feature_engineering/flink/realtime_stream_job.py` : khong dung trong KFP flow; duoc docker realtime script/Airflow full dataflow stream path dung.

`apps/data-platform/src/feature_engineering/flink/candidate_pool_job.py` : khong dung trong KFP flow; duoc `realtime_stream_job.py` import.

`apps/data-platform/src/feature_engineering/flink/item_features_job.py` : khong dung trong KFP flow; duoc `realtime_stream_job.py` import.

`apps/data-platform/src/feature_engineering/flink/user_aggregate_job.py` : khong dung trong KFP flow; duoc `realtime_stream_job.py` import.

`apps/data-platform/src/feature_engineering/flink/user_sequence_job.py` : khong dung trong KFP flow; duoc `realtime_stream_job.py` import va tests dung.

`apps/data-platform/src/feature_store/online_writer.py` : khong dung trong KFP flow; duoc `realtime_stream_job.py` dung de ghi Redis online features.

`apps/data-platform/src/ingest/bronze_cdc_reader.py` : khong dung trong KFP flow; duoc realtime Spark/Flink/validation docker flow dung.

`apps/data-platform/src/feature_store/feast_registry.py` : khong dung trong KFP flow; duoc `apps/data-platform/feature-store/src/apply_feast_repo.py` va `materialize_offline_to_online.py` dung.

`apps/data-platform/src/orchestration/airflow/dags/batch_feature_pipeline_dag.py` : khong dung trong KFP flow; Airflow-only DAG.

`apps/data-platform/src/orchestration/airflow/dags/feast_materialization_dag.py` : khong dung trong KFP flow; Airflow-only DAG.

`apps/data-platform/src/orchestration/airflow/dags/full_dataflow_local_dag.py` : khong dung trong KFP flow; docker-compose/Airflow full local dataflow DAG.

`apps/data-platform/src/orchestration/airflow/dags/raw_ingestion_dag.py` : khong dung trong KFP flow; Airflow-only raw generation/ingestion DAG.

`apps/data-platform/src/orchestration/airflow/dags/streaming_feature_pipeline_dag.py` : khong dung trong KFP flow; Airflow-only streaming DAG, hien goi `run_streaming_features.py || true`.

## Strong Candidate Unused / Prune Or Refactor

Nhung file nay khong nam trong KFP/Ray E2E va static search khong thay runtime caller ro rang.

`apps/data-platform/src/local/run_streaming_features.py` : candidate prune/refactor; hien chi raise message noi rang streaming job phai chay trong Flink service. `streaming_feature_pipeline_dag.py` goi file nay voi `|| true`, nen no dang la scaffold placeholder.

`apps/data-platform/src/ingest/kafka_raw_reader.py` : candidate prune/refactor; dinh nghia Kafka topic contracts nhung khong thay runtime import trong current code. Neu can contracts, nen connect vao smoke checks/config validation; neu khong, co the xoa.

`apps/data-platform/src/ingest/postgres_cdc_contracts.py` : candidate prune/refactor; dinh nghia source table contracts/primary keys nhung khong thay runtime import. Neu can CDC validation, nen wire vao `init_postgres_schema.py`/`validate_bronze_cdc.py`; neu khong, co the xoa.

`apps/data-platform/src/validate/data_quality_checks.py` : candidate prune/refactor; generic checks nhung khong thay runtime/test import. Nen wire vao feature engineering/KFP validation step hoac xoa.

## Test-Only / Contract-Only Right Now

`apps/data-platform/src/config/storage_paths.py` : hien duoc `tests/contract/test_docker_dataflow_contracts.py` import, nhung khong thay runtime caller. Co the giu neu muon enforce path contracts; neu khong thi merge vao config YAML/scripts.

`apps/data-platform/src/validate/feature_quality_checks.py` : hien duoc unit test import, nhung chua duoc KFP/dataflow runtime goi. Nen bien thanh KFP validation step neu muon feature drift/data quality gate.

## Thin Wrappers / Optional Convenience

`apps/data-platform/src/local/run_full_feature_flow.py` : chi wrap `run_batch_features()`. Khong dung trong KFP flow. Co the giu lam convenience CLI hoac xoa neu muon repo gon.

`apps/data-platform/src/feature_engineering/spark/spark_batch_entrypoint.py` : thin wrapper goi `run_batch_features.main`. Giu neu Airflow/Spark submit con dung; xoa neu bo docker/Airflow Spark path.

Package marker files nhu `__init__.py` khong tinh la unused business code; can giu neu package import con dung.

## Suggested Cleanup Plan

1. Neu scope tu nay tro di la **Kubeflow + Ray + MLflow only**:
   - Co the remove/archival nhom Airflow-only va realtime-only sau khi confirm khong can docker dataflow demo.
   - Giu active KFP/Ray E2E files va Spark build modules offline.

2. Neu van giu **hybrid local dataflow + KFP model training**:
   - Khong xoa Airflow/Flink/Feast files.
   - Chi xu ly candidate prune: `run_streaming_features.py`, `kafka_raw_reader.py`, `postgres_cdc_contracts.py`, `data_quality_checks.py`.

3. Nen uu tien wire validation thay vi xoa:
   - Dung `feature_quality_checks.py` lam KFP validation component.
   - Dung `data_quality_checks.py` sau `build_silver_tables`.
   - Dung CDC contracts trong `validate_bronze_cdc.py`.
