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

- [notebooks/ml.ipynb line 8 (line 8)](../../../notebooks/ml.ipynb): local Jupyter notebook for the ML workflow.
- [notebooks/ml.ipynb line 301 (line 301)](../../../notebooks/ml.ipynb): imports Feast `FeatureStore`.
- [notebooks/ml.ipynb line 328 (line 328)](../../../notebooks/ml.ipynb): calls `FeatureStore.get_historical_features(...)` for `bst_ranking_v1`.
- [notebooks/ml.ipynb line 869 (line 869)](../../../notebooks/ml.ipynb): saves the trained notebook artifact as `.joblib`.
- [apps/data-platform/feature-store/feature_repo/feature_store.yaml line 8 (line 8)](../../../apps/data-platform/feature-store/feature_repo/feature_store.yaml#L8): Feast PostgreSQL offline-store config.
- [apps/data-platform/feature-store/feature_repo/feature_store.yaml line 18 (line 18)](../../../apps/data-platform/feature-store/feature_repo/feature_store.yaml#L18): Feast Redis online-store config.
- [apps/data-platform/feature-store/feature_repo/features.py line 22 (line 22)](../../../apps/data-platform/feature-store/feature_repo/features.py#L22): PostgreSQL source for the user sequence FeatureView.
- [apps/data-platform/feature-store/feature_repo/features.py line 112 (line 112)](../../../apps/data-platform/feature-store/feature_repo/features.py#L112): FeatureService `bst_ranking_v1`.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 149 (line 149)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L149): reads PostgreSQL label/entity input.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 344 (line 344)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L344): calls Feast native historical retrieval in the production data-prep CLI.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 509 (line 509)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L509): writes BST train/validation/test JSONL splits.
- [apps/ml-system/src/models/dataset.py line 6 (line 6)](../../../apps/ml-system/src/models/dataset.py#L6): `recommenderDataset` used to load BST JSONL samples.
- [apps/ml-system/src/models/model.py line 886 (line 886)](../../../apps/ml-system/src/models/model.py#L886): `BST` recommendation model.
- [apps/ml-system/src/models/trainer.py line 58 (line 58)](../../../apps/ml-system/src/models/trainer.py#L58): training and validation loop.
- [apps/ml-system/src/training/train.py line 25 (line 25)](../../../apps/ml-system/src/training/train.py#L25): MLflow logging for model params, metrics, and artifacts.
- [apps/ml-system/src/cli/evaluate_bst.py line 67 (line 67)](../../../apps/ml-system/src/cli/evaluate_bst.py#L67): test-set evaluation entrypoint.
- [apps/ml-system/src/registry/model_promotion.py line 557 (line 557)](../../../apps/ml-system/src/registry/model_promotion.py#L557): export, MLflow model registry update, Triton layout generation, and promotion manifest upload.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 44 (line 44)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L44): Kubeflow prepare-data component.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 194 (line 194)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L194): Kubeflow promotion component.

## Notebook Main Steps

| Step | What the notebook does | Notebook code reference |
| --- | --- | --- |
| Connect to Feast offline store | Uses PostgreSQL connection settings for `feature_store.ml_ranking_labels`. When running locally, a PostgreSQL port-forward can expose the cloud service at `127.0.0.1:15432`; the data retrieval itself still uses Feast native APIs. | [notebooks/ml.ipynb line 110 (line 110)](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb line 122 (line 122)](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb line 246 (line 246)](../../../notebooks/ml.ipynb) |
| Load historical features | Reads labels as the Feast entity dataframe, then calls `FeatureStore.get_historical_features(...)` with FeatureService `bst_ranking_v1`. | [notebooks/ml.ipynb line 301 (line 301)](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb line 321 (line 321)](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb line 328 (line 328)](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb line 339 (line 339)](../../../notebooks/ml.ipynb) |
| Split train, validation, and test sets | Converts historical features to BST JSONL rows and writes temporal splits under `notebooks/data/feast_postgres_bst_split/`. | [notebooks/ml.ipynb line 350 (line 350)](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb line 450 (line 450)](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb line 529 (line 529)](../../../notebooks/ml.ipynb) |
| Train model | Trains the existing `BST` model through the existing `Trainer` class. The task is binary recommendation classification, so the proof focuses on loss, AUC, and ranking metrics rather than regression metrics. | [notebooks/ml.ipynb line 541 (line 541)](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb line 714 (line 714)](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb line 718 (line 718)](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb line 733 (line 733)](../../../notebooks/ml.ipynb) |
| Evaluate model | Runs validation/test evaluation with AUC, loss, hitrate, MRR, and NDCG metrics. | [notebooks/ml.ipynb line 758 (line 758)](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb line 795 (line 795)](../../../notebooks/ml.ipynb) |
| Save model | Saves local notebook artifact to `notebooks/models/feast_postgres_bst_10epoch.joblib` with model weights, config, metrics, Feast lineage, PostgreSQL lineage, and split metadata. | [notebooks/ml.ipynb line 819 (line 819)](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb line 869 (line 869)](../../../notebooks/ml.ipynb), [notebooks/ml.ipynb line 871 (line 871)](../../../notebooks/ml.ipynb) |

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
