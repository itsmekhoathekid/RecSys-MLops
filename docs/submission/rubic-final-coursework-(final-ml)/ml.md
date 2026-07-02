# ML

## Jupyter Notebook To Demonstrate Basic Understanding Of ML/DL

Code reference:

- [notebooks/ml.ipynb](../../../notebooks/ml.ipynb): executed notebook for the full ML workflow.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 27](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L27): default Apache Iceberg offline feature table `recsys_features.feature_store.ml_bst_training`.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 284](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L284): creates the Spark session used to read the Iceberg offline feature store.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 286](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L286): reads the offline feature table with `spark.table(...)`.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 436](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L436): selects the `offline_feature_store` path used by the notebook and Kubeflow training pipeline.
- [apps/ml-system/src/models/dataset.py line 6](../../../apps/ml-system/src/models/dataset.py#L6): existing `recommenderDataset` class used by the notebook.
- [apps/ml-system/src/models/model.py line 886](../../../apps/ml-system/src/models/model.py#L886): existing `BST` model used for training.
- [apps/ml-system/src/models/trainer.py line 58](../../../apps/ml-system/src/models/trainer.py#L58): existing `Trainer` class used for training and validation evaluation.
- [notebooks/data/iceberg_bst_split/split_meta.json](../../../notebooks/data/iceberg_bst_split/split_meta.json): metadata for the Iceberg-derived train, validation, and test JSONL splits.
- [notebooks/models/iceberg_bst_10epoch.joblib](../../../notebooks/models/iceberg_bst_10epoch.joblib): saved BST model artifact trained from Iceberg offline feature data.

## Document Main Steps Done In The Notebook

| Step | What the notebook does | Evidence |
| --- | --- | --- |
| Load data from Apache Iceberg offline feature store | Starts temporary Spark pod `recsys-iceberg-training-export`, reads `recsys_features.feature_store.ml_bst_training` from the Iceberg offline feature store, and exports BST-ready JSONL splits with `prepare_bst_training_data.py`. MinIO is only the S3-compatible storage backend for the Iceberg warehouse, not the offline-store abstraction described in this proof. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [prepare_bst_training_data.py line 286](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L286) |
| Split training, validation, and test sets | Uses the production temporal split logic from `prepare_bst_training_data.py`: sort by `prediction_timestamp` when available, otherwise `event_time`, then export `train.jsonl`, `val.jsonl`, and `test.jsonl`. Latest notebook run exported `3,893` total rows: `3,114` train, `389` validation, and `390` test. | [notebooks/data/iceberg_bst_split/split_meta.json](../../../notebooks/data/iceberg_bst_split/split_meta.json) |
| Train model on training dataset | Trains the existing `BST` model through the existing `Trainer` class for exactly 10 epochs on MPS when available. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [model.py line 886](../../../apps/ml-system/src/models/model.py#L886), [trainer.py line 58](../../../apps/ml-system/src/models/trainer.py#L58) |
| Evaluate on validation dataset | Reports validation metrics from `Trainer.evaluate`, including AUC, loss, hitrate, MRR, and NDCG. Latest notebook run finished with validation `loss=0.3125`, `auc=0.5525`, and `ndcg@10=0.0977`. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [trainer.py line 175](../../../apps/ml-system/src/models/trainer.py#L175) |
| Save model | Saves the model state, config, metrics, Iceberg table name, Iceberg warehouse, dataset metadata, split metadata, and JSONL paths as `.joblib`. | [iceberg_bst_10epoch.joblib](../../../notebooks/models/iceberg_bst_10epoch.joblib) |
