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
| Notebook proof | [ml.ipynb (line 301)](../../../notebooks/ml.ipynb#L301), [ml.ipynb (line 328)](../../../notebooks/ml.ipynb#L328) — Feast retrieval; [ml.ipynb (line 365)](../../../notebooks/ml.ipynb#L365), [ml.ipynb (line 475)](../../../notebooks/ml.ipynb#L475), [ml.ipynb (line 718)](../../../notebooks/ml.ipynb#L718), [ml.ipynb (line 803)](../../../notebooks/ml.ipynb#L803), and [ml.ipynb (line 806)](../../../notebooks/ml.ipynb#L806), [ml.ipynb (line 869)](../../../notebooks/ml.ipynb#L869). |
| Feast stores and feature service | [feature_store.yaml (line 1)](../../../apps/data-platform/feature-store/feature_repo/feature_store.yaml#L1), [feature_store.yaml (line 20)](../../../apps/data-platform/feature-store/feature_repo/feature_store.yaml#L20), [features.py (line 18)](../../../apps/data-platform/feature-store/feature_repo/features.py#L18), [features.py (line 120)](../../../apps/data-platform/feature-store/feature_repo/features.py#L120) |
| Production data preparation | [prepare_bst_training_data.py (line 312)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L312), [prepare_bst_training_data.py (line 758)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L758) — PostgreSQL labels, Feast historical features, and temporal JSONL splits. |
| Dataset, model, and trainer | [dataset.py (line 8)](../../../apps/ml-system/src/models/dataset.py#L8), [dataset.py (line 73)](../../../apps/ml-system/src/models/dataset.py#L73), [model.py (line 856)](../../../apps/ml-system/src/models/model.py#L856), [model.py (line 1081)](../../../apps/ml-system/src/models/model.py#L1081), [trainer.py (line 58)](../../../apps/ml-system/src/models/trainer.py#L58), [trainer.py (line 295)](../../../apps/ml-system/src/models/trainer.py#L295) |
| Evaluation and MLflow logging | [evaluate_bst.py (line 14)](../../../apps/ml-system/src/cli/evaluate_bst.py#L14), [evaluate_bst.py (line 65)](../../../apps/ml-system/src/cli/evaluate_bst.py#L65), [train.py (line 25)](../../../apps/ml-system/src/training/train.py#L25), [train.py (line 146)](../../../apps/ml-system/src/training/train.py#L146) |
| Promotion and orchestration | [model_promotion.py (line 405)](../../../apps/ml-system/src/registry/model_promotion.py#L405), [model_promotion.py (line 666)](../../../apps/ml-system/src/registry/model_promotion.py#L666), [bst_training_pipeline.py (line 280)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L280), [bst_training_pipeline.py (line 470)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L470) |

## Notebook Main Steps

| Step | What the notebook does | Notebook code reference |
| --- | --- | --- |
| Connect to Feast offline store | Uses PostgreSQL connection settings for `feature_store.ml_ranking_labels`. When running locally, a PostgreSQL port-forward can expose the cloud service at `127.0.0.1:15432`; the data retrieval itself still uses Feast native APIs. | [ml.ipynb (line 110)](../../../notebooks/ml.ipynb#L110), [ml.ipynb (line 136)](../../../notebooks/ml.ipynb#L136), [ml.ipynb (line 171)](../../../notebooks/ml.ipynb#L171), [ml.ipynb (line 212)](../../../notebooks/ml.ipynb#L212) |
| Load historical features | Reads labels as the Feast entity dataframe, then calls `FeatureStore.get_historical_features(...)` with FeatureService `bst_ranking_v1`. | [ml.ipynb (line 301)](../../../notebooks/ml.ipynb#L301), [ml.ipynb (line 328)](../../../notebooks/ml.ipynb#L328) |
| Split train, validation, and test sets | Converts historical features to BST JSONL rows and writes temporal splits under `notebooks/data/feast_postgres_bst_split/`. | [ml.ipynb (line 365)](../../../notebooks/ml.ipynb#L365), [ml.ipynb (line 475)](../../../notebooks/ml.ipynb#L475) |
| Train model | Trains the existing `BST` model through the existing `Trainer` class. The task is binary recommendation classification, so the proof focuses on loss, AUC, and ranking metrics rather than regression metrics. | [ml.ipynb (line 650)](../../../notebooks/ml.ipynb#L650), [ml.ipynb (line 755)](../../../notebooks/ml.ipynb#L755) |
| Evaluate model | Runs validation/test evaluation with AUC, loss, hitrate, MRR, and NDCG metrics. | [ml.ipynb (line 718)](../../../notebooks/ml.ipynb#L718), [ml.ipynb (line 803)](../../../notebooks/ml.ipynb#L803) |
| Save model | Saves local notebook artifact to `notebooks/models/feast_postgres_bst_10epoch.joblib` with model weights, config, metrics, Feast lineage, PostgreSQL lineage, and split metadata. | [ml.ipynb (line 806)](../../../notebooks/ml.ipynb#L806), [ml.ipynb (line 869)](../../../notebooks/ml.ipynb#L869) |

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
