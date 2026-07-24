# Kubeflow Feature Engineering And Training Plan

Operational runbook for local deploy/run/log monitoring:

- `docs/kubeflow_mlflow_ray_local_runbook.md`

## Feature Engineering Gap

Feature store hien co 3 nhom feature chinh:

- `user_sequence_features`: lich su item/event/category/brand/price/time theo user.
- `user_aggregate_features`: views/carts/purchases va cac aggregate gan realtime theo user.
- `item_features`: category/brand/price bucket, popularity, conversion va activity theo item.

Nhung de train BST trong `apps/ml-system/src/models/`, can them cac buoc sau:

- Build training labels theo impression/candidate tai `prediction_timestamp`.
- Point-in-time join labels voi sequence, user aggregate va item features de tranh leakage.
- Convert `ml_bst_training` thanh JSONL split theo format `recommenderDataset` dang doc.
- Chia split theo thoi gian `train/val/test`, khong shuffle truoc split.
- Validate feature quality truoc train: null rate, sequence length, label balance, feature freshness, entity coverage.
- Track feature version/run id de moi model config biet no train tu feature snapshot nao.

Code moi da them:

- `apps/ml-system/src/cli/prepare_bst_training_data.py`: doc `ml_bst_training` parquet tu local/S3 MinIO va tao `train.jsonl`, `val.jsonl`, `test.jsonl`.
- `apps/ml-system/src/cli/evaluate_bst.py`: load checkpoint BST va tinh metric tren split test.
- `apps/ml-system/src/training/train.py`: them metrics output, MLflow logging, MinIO artifact logging qua MLflow artifact store, va Postgres model config registry.

## Kubeflow Flow

Target flow:

1. `feature_engineering`: chay `apps/data-platform/src/features/spark/spark_batch_entrypoint.py` de tao silver tables, Feast offline features, labels va `ml_bst_training`.
2. `prepare_training_data`: convert `ml_bst_training` parquet sang JSONL split cho `apps/ml-system/src/models/dataset.py`.
3. `submit_rayjob`: submit KubeRay `RayJob`; Ray Tune chay HPO va Ray workers train BST trials.
4. `evaluate_bst`: evaluate best Ray checkpoint tren test split, log test metrics vao MLflow run.
5. Artifact/config:
   - Model weight checkpoint: MLflow artifact store tren MinIO.
   - Model config/metrics/artifact URI: bang `model_configs` trong Postgres.

KFP DSL nam o:

- `apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py`

## Helm Charts

Charts moi:

- `infra/helm/mlflow-stack`: MLflow tracking server, MinIO, PostgreSQL.
- `infra/helm/recsys-runtime`: PVC va secret runtime cho pipeline pod.
- `infra/helm/ray-cluster`: RayJob CPU profile va GPU overlay cho KubeRay.

Build images:

```bash
docker build -f infra/docker/Dockerfile.base-python -t recsys-base-python:local .
docker build -f apps/ml-system/Dockerfile.training -t recsys-mlops-training:local .
docker build -f infra/docker/Dockerfile.mlflow -t recsys-mlflow:local .
```

Install KubeRay operator:

```bash
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo update
helm upgrade --install kuberay-operator kuberay/kuberay-operator \
  --namespace kubeflow \
  --create-namespace
```

Install MLflow stack:

```bash
helm upgrade --install recsys-mlflow infra/helm/mlflow-stack \
  --namespace experiment-tracking \
  --create-namespace
```

Install runtime secret/PVC in the Kubeflow user namespace:

```bash
helm upgrade --install recsys-runtime infra/helm/recsys-runtime \
  --namespace kubeflow \
  --set namespace.name=kubeflow
```

Compile KFP pipeline:

```bash
RECSYS_PIPELINE_IMAGE=recsys-mlops-training:local \
uv run python apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py
```

Submit `infra/kubeflow/compiled/bst_training_pipeline.yaml` in Kubeflow Pipelines UI.

## Retraining Triggers

Nen trigger retraining khi:

- Feature drift: distribution cua `views_30m`, `purchases_24h`, `popularity_score`, sequence length, label rate lech qua nguong.
- Data freshness: feature tables khong co partition moi trong expected SLA.
- Model metric drop: `ndcg@10`, `gauc`, `hitrate@10` giam so voi production baseline.
- Coverage drop: ty le user/item khong join duoc feature tang bat thuong.

## Next Hardening

- Them component data validation thanh step rieng trong KFP.
- Tach train/eval output path sang PVC mount path `/opt/recsys` hoac S3 path thong nhat.
- Build image dry-run trong Jenkins CI/CD; push registry/deploy cluster se la hardening phase sau.
- Them model promotion gate: chi promote khi test `ndcg@10`/`gauc` vuot threshold.
- Neu can distributed tuning/training, them Ray/KubeRay chart sau khi single-node flow on dinh.
