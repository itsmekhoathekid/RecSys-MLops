# Kubeflow To KubeRay Flow Explained

Tai lieu nay dien giai tung buoc trong flow:

```text
Kubeflow Pipelines
  -> Feature Engineering
  -> Prepare Training Data
  -> Submit KubeRay RayJob
  -> Ray Tune + Training
  -> MLflow/MinIO/Postgres
  -> Evaluation
```

Muc tieu la lam ro:

- Component nao dam bao nhiem vu gi.
- Data/artifact di qua dau.
- Kubeflow va KubeRay chia viec voi nhau nhu the nao.

## 0. Big Picture

```text
KFP pipeline
  |
  |-- step 1: feature_engineering
  |     component: KFP container pod
  |     job: build feature tables va ml_bst_training
  |
  |-- step 2: prepare_training_data
  |     component: KFP container pod
  |     job: convert training table thanh train/val/test JSONL
  |
  |-- step 3: submit_rayjob
  |     component: KFP container pod + Kubernetes API
  |     job: tao RayJob CRD
  |
  |-- KubeRay operator
  |     component: Kubernetes operator
  |     job: watch RayJob, tao RayCluster, head pod, worker pod, driver job
  |
  |-- Ray Tune
  |     component: Ray library chay trong Ray cluster
  |     job: schedule HPO trials tren Ray worker
  |
  |-- train.py
  |     component: model training code cua project
  |     job: train BST model cho moi trial
  |
  |-- MLflow + MinIO + Postgres
  |     component: tracking/artifact/registry stack
  |     job: luu params, metrics, model artifact, model config
  |
  |-- step 4: evaluate_bst
        component: KFP container pod
        job: evaluate best checkpoint tren test split
```

## 1. Kubeflow Pipeline Orchestrates Workflow

Main file:

```text
apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py
```

Compiled pipeline:

```text
infra/kubeflow/compiled/bst_training_pipeline.yaml
```

Kubeflow Pipelines dam bao:

- Chay dung thu tu cac step.
- Tao pod cho tung container component.
- Mount shared PVC vao moi step.
- Inject runtime secret vao moi step.
- Track status step success/fail.
- Cho phep xem run trong KFP UI.

Kubeflow Pipelines khong dam bao:

- Khong tu scale Ray workers.
- Khong tu chay HPO logic.
- Khong tu train model.
- Khong tu luu model artifact vao MinIO.

Nhung viec do duoc giao cho KubeRay, Ray Tune, training code va MLflow stack.

## 2. Runtime Resources Shared By All Steps

Main chart:

```text
infra/helm/recsys-runtime
```

Important resources:

```text
recsys-mlops-pvc
recsys-mlops-runtime
pipeline-runner RBAC
```

### PVC

File:

```text
infra/helm/recsys-runtime/templates/pvc.yaml
```

Nhiem vu:

- Tao shared volume cho KFP steps va Ray pods.
- Dung lam workspace chung tai mount path:

```text
/workspace
```

Trong flow hien tai, data va artifact trung gian nam o:

```text
/workspace/recsys/data_platform/output
/workspace/recsys/notebooks/data/bst_split
/workspace/recsys/data_platform/output/ml/ray
```

### Runtime Secret

File:

```text
infra/helm/recsys-runtime/templates/secret.yaml
```

Nhiem vu:

- Inject MLflow URI.
- Inject MinIO credentials.
- Inject Postgres model registry URI.
- Inject S3-compatible env vars cho MLflow artifact upload.

Env vars quan trong:

```text
MLFLOW_TRACKING_URI
MLFLOW_EXPERIMENT_NAME
MLFLOW_S3_ENDPOINT_URL
MODEL_REGISTRY_POSTGRES_URI
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
```

### RBAC

File:

```text
infra/helm/recsys-runtime/templates/rbac.yaml
```

Nhiem vu:

- Cho KFP pod quyen create/get/watch/delete `RayJob`.
- Bind role vao service account `pipeline-runner`.

Neu thieu RBAC, step `submit_rayjob` se fail khi goi Kubernetes API.

## 3. Step 1: Feature Engineering

KFP component:

```text
feature_engineering
```

Defined in:

```text
apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py
```

Command chay trong pod:

```bash
python -m recsys_model_pipeline.run_feature_engineering
```

Entrypoint:

```text
apps/ml-system/src/run_feature_engineering.py
```

File nay dam bao:

- Load source config tu `configs/local/spark_batch.yaml`.
- Rewrite runtime output path sang PVC path.
- Goi local batch feature flow.
- Ghi summary JSON neu duoc yeu cau.

Nó gọi:

```text
apps/data-platform/src/local/run_batch_features.py
```

`run_batch_features.py` dam bao:

- Doc raw/generated run path.
- Build silver tables.
- Build user sequence features.
- Build user aggregate features.
- Build item features.
- Build ranking labels.
- Build BST training table.
- Ghi feature/offline/ml tables ra storage.

Sub-components:

```text
apps/data-platform/src/feature_engineering/spark/build_silver_tables.py
apps/data-platform/src/feature_engineering/spark/build_user_sequence_features.py
apps/data-platform/src/feature_engineering/spark/build_user_aggregate_features.py
apps/data-platform/src/feature_engineering/spark/build_item_features.py
apps/data-platform/src/feature_engineering/spark/build_ranking_labels.py
apps/data-platform/src/feature_engineering/spark/build_bst_training_table.py
```

Output chinh:

```text
/workspace/recsys/data_platform/output/silver/*
/workspace/recsys/data_platform/output/feature_store/offline/*
/workspace/recsys/data_platform/output/ml/offline/ml_ranking_labels
/workspace/recsys/data_platform/output/ml/offline/ml_bst_training
/workspace/recsys/data_platform/output/feature_summary.json
```

Important note:

```text
Hien tai step nay la KFP container component binh thuong.
No chua dung Kubeflow Spark Operator.
```

Ten folder `feature_engineering/spark` the hien logic batch/Spark-style, nhung local/KFP smoke hien tai chay Python function truc tiep trong container.

## 4. Step 2: Prepare Training Data

KFP component:

```text
prepare_training_data
```

Command chay trong pod:

```bash
python -m recsys_model_pipeline.prepare_bst_training_data
```

Entrypoint:

```text
apps/ml-system/src/prepare_bst_training_data.py
```

Input:

```text
/workspace/recsys/data_platform/output/ml/offline/ml_bst_training
```

Component nay dam bao:

- Doc training table tu parquet/offline feature path.
- Normalize row format cho `apps/ml-system/src/models/dataset.py`.
- Cat sequence fields ve max history length.
- Split temporal train/val/test.
- Ghi JSONL files cho model training.

Output:

```text
/workspace/recsys/notebooks/data/bst_split/train.jsonl
/workspace/recsys/notebooks/data/bst_split/val.jsonl
/workspace/recsys/notebooks/data/bst_split/test.jsonl
/workspace/recsys/notebooks/data/bst_split/split_meta.json
```

Model dataset file doc cac JSONL nay:

```text
apps/ml-system/src/models/dataset.py
```

## 5. Step 3: Kubeflow Submits RayJob

KFP component:

```text
submit_rayjob
```

Command chay trong pod:

```bash
python -m recsys_model_pipeline.submit_ray_job
```

Entrypoint:

```text
apps/ml-system/src/submit_ray_job.py
```

Component nay dam bao:

- Build RayJob spec.
- Gan image training.
- Gan PVC mount `/workspace`.
- Gan runtime secret.
- Gan CPU/memory resources cho Ray head va Ray worker.
- Neu `use_gpu=true`, gan GPU request/limit cho worker.
- Goi Kubernetes API tao `RayJob`.
- Poll RayJob status den khi `SUCCEEDED` hoac `FAILED`.
- Ghi status JSON neu co `--status-path`.

Important concept:

```text
RayJob la mot Kubernetes Custom Resource do KubeRay operator cung cap.
```

Kubernetes native co:

```text
Pod
Deployment
Service
Job
PVC
Secret
```

KubeRay cai them:

```text
RayJob
RayCluster
RayService
```

Vay khi KFP submit `RayJob`, no khong tu tao Ray pods. No chi tao mot custom resource nhu:

```yaml
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: recsys-bst-ray-tune
spec:
  entrypoint: python -m recsys_model_pipeline.ray_tune_train_bst ...
  rayClusterSpec:
    headGroupSpec: ...
    workerGroupSpecs: ...
```

## 6. KubeRay Operator Handles RayJob

Installed by:

```text
make mlops-install-kuberay
```

Operator deployment:

```text
kuberay-operator
```

Namespace:

```text
kubeflow
```

KubeRay operator dam bao:

- Watch `RayJob` resources.
- Tao `RayCluster` tu `rayClusterSpec`.
- Tao Ray head pod.
- Tao Ray worker pod(s).
- Tao Ray head service/dashboard service.
- Submit entrypoint vao Ray Jobs API.
- Update RayJob status:

```text
PENDING
RUNNING
SUCCEEDED
FAILED
```

KubeRay operator khong dam bao:

- Khong chon hyperparameters.
- Khong implement training loop.
- Khong tinh metrics model.

Nhung viec do nam trong Ray Tune va project training code.

## 7. RayCluster Components

Khi RayJob duoc tao, KubeRay tao RayCluster.

### Ray Head Pod

Container name:

```text
ray-head
```

Nhiem vu:

- Khoi dong Ray cluster.
- Chay Ray dashboard.
- Nhan Ray Jobs submission.
- Quan ly cluster metadata/scheduler.
- Lam endpoint cho driver connect vao Ray cluster.

Important config:

```text
rayStartParams:
  dashboard-host: "0.0.0.0"
  num-cpus: "0"
```

`num-cpus: 0` giup head khong nhan trial compute. Compute nen chay tren worker.

### Ray Worker Pod

Container name:

```text
ray-worker
```

Nhiem vu:

- Chay Ray Tune trial tasks.
- Thuc thi model training/evaluation cua tung trial.
- Doc train/val/test data tu PVC.
- Log MLflow artifacts/metrics tu training code.

CPU smoke profile:

```text
worker replicas: 1
worker cpu: 500m request, 2 cpu limit
worker memory: 1Gi request, 2Gi limit
```

GPU later:

```text
nvidia.com/gpu: 1
nodeSelector:
  nvidia.com/gpu.present: "true"
```

## 8. RayJob Entrypoint Runs Ray Tune

RayJob entrypoint:

```bash
python -m recsys_model_pipeline.ray_tune_train_bst
```

File:

```text
apps/ml-system/src/ray_tune_train_bst.py
```

This file dam bao:

- Load base BST config.
- Define search space.
- Scan split data de set embedding cardinalities dung.
- Build trial-specific config.
- Start Ray Tune tuner.
- Run training function cho moi trial.
- Report objective metric ve Ray Tune.
- Chon best trial.
- Ghi `best_result.json`.
- Register best model config vao Postgres.

Search space hien tai:

```text
learning_rate
weight_decay
hidden_dropout_prob
```

Smoke defaults:

```text
max_trials=2
parallel_trials=1
num_epochs=1
training_percent=0.01
objective=val/ndcg@10
```

## 9. One Ray Tune Trial Does Training

Trong `ray_tune_train_bst.py`, moi trial lam cac viec:

```text
1. Tao trial dir tren PVC
2. Tao bst_trial.yaml rieng
3. Set hyperparameters cua trial
4. Set train/val/test JSONL paths
5. Goi run_training trong train.py
6. Log metrics/artifacts vao MLflow
7. Report metrics ve Ray Tune
```

Trial output path:

```text
/workspace/recsys/data_platform/output/ml/ray/trials/<trial_name>/
```

Trong do co:

```text
bst_trial.yaml
training_result.json
checkpoints/BST
```

## 10. Training Code Responsibilities

Main training file:

```text
train.py
```

Model files:

```text
apps/ml-system/src/models/dataset.py
apps/ml-system/src/models/model.py
apps/ml-system/src/models/trainer.py
```

`apps/ml-system/src/train.py` dam bao:

- Load config.
- Tao trainer.
- Tao train/val dataset.
- Run training loop.
- Evaluate validation metrics.
- Save best checkpoint.
- Log params/metrics/model artifact vao MLflow.
- Optionally register model config vao Postgres.

`apps/ml-system/src/models/dataset.py` dam bao:

- Doc JSONL split.
- Convert row thanh tensors/features.
- Apply padding/history sequence handling.

`apps/ml-system/src/models/model.py` dam bao:

- Define BST architecture.
- Forward pass cho behavior sequence va target item.

`apps/ml-system/src/models/trainer.py` dam bao:

- Dataloader.
- Forward batch.
- Loss computation.
- Metrics computation: AUC, hitrate, NDCG, MRR, GAUC.
- Checkpoint save/load.

## 11. MLflow, MinIO, Postgres Responsibilities

MLflow stack chart:

```text
infra/helm/mlflow-stack
```

### MLflow

File:

```text
infra/helm/mlflow-stack/templates/mlflow.yaml
```

MLflow dam bao:

- Track experiment.
- Store run params.
- Store run metrics.
- Store artifact metadata.
- Expose MLflow UI.

### MinIO

File:

```text
infra/helm/mlflow-stack/templates/minio.yaml
```

MinIO dam bao:

- S3-compatible artifact storage.
- Luu model checkpoint.
- Luu config/metrics artifacts cua MLflow runs.

Artifact URI example:

```text
s3://mlflow-artifacts/<experiment_id>/<run_id>/artifacts/model
```

### Postgres

File:

```text
infra/helm/mlflow-stack/templates/postgres.yaml
```

Postgres dam bao:

- MLflow backend database.
- Custom table `model_configs`.
- Luu best model config, metrics, artifact URI.

Registry helper:

```text
apps/ml-system/src/model_registry.py
```

## 12. Best Result Contract

Ray Tune ghi best result vao:

```text
/workspace/recsys/data_platform/output/ml/ray/best_result.json
```

File nay la contract giua:

```text
Ray Tune/Training step
  -> Evaluation step
```

No chua:

```text
best_config
best_config_path
best_trial_name
checkpoint_path
artifact_uri
metrics
ray_metrics
```

Evaluation step khong can biet Ray Tune chay bao nhieu trial. No chi doc `best_result.json`, lay best checkpoint va evaluate.

## 13. Step 4: Evaluation

KFP component:

```text
evaluate_bst
```

Command:

```bash
python -m recsys_model_pipeline.evaluate_ray_best_bst
```

Wrapper:

```text
apps/ml-system/src/evaluate_ray_best_bst.py
```

It calls:

```text
apps/ml-system/src/evaluate_bst.py
```

Evaluation step dam bao:

- Doc `best_result.json`.
- Lay `best_config_path`.
- Lay `checkpoint_path`.
- Load model checkpoint.
- Evaluate tren `test.jsonl`.
- Ghi test metrics JSON.
- Log test metrics vao MLflow neu tracking URI co san.

Output:

```text
/workspace/recsys/data_platform/output/ml/eval_metrics.json
```

## 14. End-To-End Responsibility Table

| Stage | Component | Responsibility |
| --- | --- | --- |
| Workflow orchestration | Kubeflow Pipelines | Chay step theo DAG, mount PVC, inject secret, track status |
| Shared workspace | PVC `recsys-mlops-pvc` | Luu feature outputs, JSONL splits, Ray outputs, checkpoints |
| Runtime credentials | Secret `recsys-mlops-runtime` | Cung cap MLflow, MinIO, Postgres env |
| Feature engineering | KFP container pod | Build feature tables va `ml_bst_training` |
| Data prep | KFP container pod | Convert training table thanh train/val/test JSONL |
| Ray submit | KFP container pod | Tao `RayJob` CRD bang Kubernetes API |
| Ray resource management | KubeRay operator | Tao RayCluster, head pod, worker pod, driver job |
| Tuning | Ray Tune | Generate trials, schedule trials, compare objective metric |
| Training | `apps/ml-system/src/train.py` + `apps/ml-system/src/models/*` | Train BST model trong tung trial |
| Tracking | MLflow | Luu params, metrics, run metadata |
| Artifact storage | MinIO | Luu checkpoint/config/metrics artifacts |
| Model config registry | Postgres | Luu best model config va artifact URI |
| Evaluation | KFP container pod | Evaluate best checkpoint tren test split |

## 15. What Is Not Used Yet

Current flow does not use:

```text
Kubeflow Spark Operator
SparkApplication CRD
Distributed Spark executors on K8s
Katib
```

Current flow uses:

```text
KFP container components for feature engineering and data prep
KubeRay RayJob for tuning + training
Ray Tune for hyperparameter search
MLflow + MinIO + Postgres for tracking/artifacts/registry
```

If moving feature engineering to Spark Operator later, Step 1 would change from:

```text
KFP container pod runs Python feature functions
```

to:

```text
KFP step submits SparkApplication CRD
Spark Operator creates Spark driver/executor pods
Spark job writes feature tables to MinIO/PVC
KFP waits for SparkApplication success
```

Ray tuning/training flow can remain the same.
