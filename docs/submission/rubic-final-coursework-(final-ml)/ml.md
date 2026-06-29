# ML

## Jupyter Notebook To Demonstrate Basic Understanding Of ML/DL

Code reference:

- [notebooks/ml.ipynb](../../../notebooks/ml.ipynb): executed notebook for the full ML workflow.
- [notebooks/export_cluster_feast_training_data.py](../../../notebooks/export_cluster_feast_training_data.py): helper copied into a K8s pod to run Feast against the cluster MinIO offline store.
- [apps/ml-system/src/models/dataset.py line 6](../../../apps/ml-system/src/models/dataset.py#L6): existing `recommenderDataset` class used by the notebook.
- [apps/ml-system/src/models/model.py line 886](../../../apps/ml-system/src/models/model.py#L886): existing `BST` model used for training.
- [apps/ml-system/src/models/trainer.py line 58](../../../apps/ml-system/src/models/trainer.py#L58): existing `Trainer` class used for training and validation evaluation.
- [notebooks/data/cluster_feast_training_dataset.parquet](../../../notebooks/data/cluster_feast_training_dataset.parquet): merged training dataset exported from Feast running inside the K8s pod.
- [notebooks/models/feast_cluster_bst_10epoch.joblib](../../../notebooks/models/feast_cluster_bst_10epoch.joblib): saved BST model artifact.

## Document Main Steps Done In The Notebook

| Step | What the notebook does | Evidence |
| --- | --- | --- |
| Load data from offline store through Feast and merge labels | Runs Feast inside K8s pod `recsys-feast-training-export`, pulls cluster offline data from MinIO, calls `FeatureStore.get_historical_features`, builds generated labels from Improve Data Generator, and merges by `user_id`. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb) |
| Split training and validation sets | Converts Feast output into `recommenderDataset` JSONL format, then uses a stratified 80/20 train-validation split. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb) |
| Train model on training dataset | Trains the existing `BST` model through the existing `Trainer` class for exactly 10 epochs on MPS when available. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [model.py line 886](../../../apps/ml-system/src/models/model.py#L886), [trainer.py line 58](../../../apps/ml-system/src/models/trainer.py#L58) |
| Evaluate on validation dataset | Reports validation metrics from `Trainer.evaluate`, including AUC, loss, hitrate, MRR, and NDCG. | [notebooks/ml.ipynb](../../../notebooks/ml.ipynb), [trainer.py line 175](../../../apps/ml-system/src/models/trainer.py#L175) |
| Save model | Saves the model state, config, metrics, feature lineage, and dataset paths as `.joblib`. | [feast_cluster_bst_10epoch.joblib](../../../notebooks/models/feast_cluster_bst_10epoch.joblib) |
