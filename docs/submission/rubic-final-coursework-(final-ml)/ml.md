# ML

## Jupyter Notebook To Demonstrate Basic Understanding Of ML/DL

This section demonstrates the recommendation model workflow end to end:

- Load training labels from the PostgreSQL Feast offline store.
- Retrieve point-in-time features through native Feast `FeatureStore.get_historical_features(...)`.
- Split data into train, validation, and test sets.
- Train a BST ranking model for binary recommendation classification.
- Evaluate on validation/test data.
- Save a local notebook artifact and promote the cluster-trained model through MLflow and Triton.

The Feast offline store is PostgreSQL and the online store is Redis.

## Code Reference

| Workflow step | Code reference |
| --- | --- |
| Notebook proof | [`notebooks/ml.ipynb`](../../../notebooks/ml.ipynb) — Feast retrieval, split, BST training/evaluation, and `.joblib` output. |
| Feast stores and feature service | [`feature_store.yaml`](../../../apps/data-platform/feature-store/feature_repo/feature_store.yaml), [`features.py`](../../../apps/data-platform/feature-store/feature_repo/features.py) |
| Production data preparation | [`prepare_bst_training_data.py`](../../../apps/ml-system/src/cli/prepare_bst_training_data.py) — PostgreSQL labels, Feast historical features, and temporal JSONL splits. |
| Dataset, model, and trainer | [`dataset.py`](../../../apps/ml-system/src/models/dataset.py), [`model.py`](../../../apps/ml-system/src/models/model.py), [`trainer.py`](../../../apps/ml-system/src/models/trainer.py) |
| Evaluation and MLflow logging | [`evaluate_bst.py`](../../../apps/ml-system/src/cli/evaluate_bst.py), [`train.py`](../../../apps/ml-system/src/training/train.py) |
| Promotion and orchestration | [`model_promotion.py`](../../../apps/ml-system/src/registry/model_promotion.py), [`bst_training_pipeline.py`](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py) |

## Notebook Main Steps

| Step | What the notebook does | Notebook code reference |
| --- | --- | --- |
| Connect to Feast offline store | Uses PostgreSQL connection settings for `feature_store.ml_ranking_labels`. When running locally, a PostgreSQL port-forward can expose the cloud service at `127.0.0.1:15432`; the data retrieval itself still uses Feast native APIs. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb](../../../notebooks/ml.ipynb) |
| Load historical features | Reads labels as the Feast entity dataframe, then calls `FeatureStore.get_historical_features(...)` with FeatureService `bst_ranking_v1`. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb](../../../notebooks/ml.ipynb) |
| Split train, validation, and test sets | Converts historical features to BST JSONL rows and writes temporal splits under `notebooks/data/feast_postgres_bst_split/`. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb](../../../notebooks/ml.ipynb) |
| Train model | Trains the existing `BST` model through the existing `Trainer` class. The task is binary recommendation classification, so the proof focuses on loss, AUC, and ranking metrics rather than regression metrics. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb](../../../notebooks/ml.ipynb) |
| Evaluate model | Runs validation/test evaluation with AUC, loss, hitrate, MRR, and NDCG metrics. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb](../../../notebooks/ml.ipynb) |
| Save model | Saves local notebook artifact to `notebooks/models/feast_postgres_bst_10epoch.joblib` with model weights, config, metrics, Feast lineage, PostgreSQL lineage, and split metadata. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb](../../../notebooks/ml.ipynb) |

## Latest Cluster Proof

The latest full ML proof job used Feast PostgreSQL offline-store data and completed successfully:

| Item | Latest proof value |
| --- | --- |
| Kubernetes job | `ml-postgres-proof-1700` in namespace `kubeflow` succeeded |
| Feast entity input | `postgresql://feature-postgres.recsys-dataflow.svc.cluster.local:5432/feature_store/feature_store.ml_ranking_labels` |
| Feature service | `bst_ranking_v1` |
| Historical dataset rows | `3,968` total |
| Train / validation / test split | `3,174 / 396 / 398` |
| MLflow run id | `4cd704665a54497d823b13556afa2afb` |
| MLflow artifact URI | `s3://mlflow-artifacts/1/4cd704665a54497d823b13556afa2afb/artifacts/model` |
| Validation metrics | `auc=0.5076628352`, `loss=0.4894941747`, `ndcg@10=0.125` |
| Test metrics | `auc=0.5046799207`, `loss=0.3227114127`, `ndcg@10=0.1030150754` |
| Registered model | `recsys_bst_ranker` version `5`, status `READY` |
| Promoted model version | `postgres-proof-20260703-1700` |
| Triton serving URI | `s3://recsys-model-store/triton/bst/latest` |
| Promotion manifest | `s3://recsys-model-store/promotions/bst/latest.json` |

Direct Triton inference against `bst_ensemble` returned FP32 scores for candidate ids `[1, 2, 3, 4, 5]`:

```text
[0.5361106396, 0.5486948490, 0.5339187384, 0.5082055330, 0.5118789673]
```
