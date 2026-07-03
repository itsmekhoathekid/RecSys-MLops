# ML

## Jupyter Notebook To Demonstrate Basic Understanding Of ML/DL

Code reference:

- [notebooks/ml.ipynb](../../../notebooks/ml.ipynb): executed notebook for the full ML workflow.
- [apps/data-platform/feature-store/feature_repo/feature_store.yaml](../../../apps/data-platform/feature-store/feature_repo/feature_store.yaml): Feast core offline store is configured as BigQuery.
- [apps/data-platform/feature-store/feature_repo/features.py](../../../apps/data-platform/feature-store/feature_repo/features.py): feature views use Feast `BigQuerySource`.
- [apps/ml-system/src/cli/prepare_bst_training_data.py](../../../apps/ml-system/src/cli/prepare_bst_training_data.py): supports `bigquery://...` entity/label input and then calls Feast `get_historical_features`.
- [apps/ml-system/src/models/dataset.py line 6](../../../apps/ml-system/src/models/dataset.py#L6): existing `recommenderDataset` class used by the notebook.
- [apps/ml-system/src/models/model.py line 886](../../../apps/ml-system/src/models/model.py#L886): existing `BST` model used for training.
- [apps/ml-system/src/models/trainer.py line 58](../../../apps/ml-system/src/models/trainer.py#L58): existing `Trainer` class used for training and validation evaluation.
- [notebooks/data/feast_bigquery_bst_split/split_meta.json](../../../notebooks/data/feast_bigquery_bst_split/split_meta.json): metadata for the Feast BigQuery-derived train, validation, and test JSONL splits.
- [notebooks/models/feast_bigquery_bst_10epoch.joblib](../../../notebooks/models/feast_bigquery_bst_10epoch.joblib): saved BST model artifact trained from Feast BigQuery offline-store data.

## Document Main Steps Done In The Notebook

| Step | What the notebook does | Evidence |
| --- | --- | --- |
| Load data through Feast BigQuery offline store | Reads generated labels from `bigquery://fsds-coursework.feature_store.ml_ranking_labels`, then calls native Feast `FeatureStore.get_historical_features(...)` with FeatureService `bst_ranking_v1`. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [prepare_bst_training_data.py](../../../apps/ml-system/src/cli/prepare_bst_training_data.py) |
| Split training, validation, and test sets | Uses the production temporal split logic from `prepare_bst_training_data.py`: sort by `prediction_timestamp` when available, otherwise `event_time`, then export `train.jsonl`, `val.jsonl`, and `test.jsonl`. Latest notebook run exported `3,893` total rows: `3,114` train, `389` validation, and `390` test. | [notebooks/data/iceberg_bst_split/split_meta.json](../../../notebooks/data/iceberg_bst_split/split_meta.json) |
| Train model on training dataset | Trains the existing `BST` model through the existing `Trainer` class for exactly 10 epochs on MPS when available. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [model.py line 886](../../../apps/ml-system/src/models/model.py#L886), [trainer.py line 58](../../../apps/ml-system/src/models/trainer.py#L58) |
| Evaluate on validation dataset | Reports validation metrics from `Trainer.evaluate`, including AUC, loss, hitrate, MRR, and NDCG. Latest notebook run finished with validation `loss=0.3125`, `auc=0.5525`, and `ndcg@10=0.0977`. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [trainer.py line 175](../../../apps/ml-system/src/models/trainer.py#L175) |
| Save model | Saves the model state, config, metrics, BigQuery table lineage, dataset metadata, split metadata, and JSONL paths as `.joblib`. | [feast_bigquery_bst_10epoch.joblib](../../../notebooks/models/feast_bigquery_bst_10epoch.joblib) |
