# Versioning

## Model Versioning

### Code reference

- [apps/ml-system/src/training/train.py line 25 (line 25)](../../../apps/ml-system/src/training/train.py#L25): logs BST training parameters, metrics, checkpoint artifact, config, and dataset lineage into MLflow.
- [apps/ml-system/src/training/train.py line 54 (line 54)](../../../apps/ml-system/src/training/train.py#L54): registers model metadata into the PostgreSQL model registry table after training.
- [apps/ml-system/src/training/train.py line 131 (line 131)](../../../apps/ml-system/src/training/train.py#L131): returns the MLflow run ID and model artifact URI from the training run.
- [apps/ml-system/src/training/ray_tune_train_bst.py line 175 (line 175)](../../../apps/ml-system/src/training/ray_tune_train_bst.py#L175): registers the best Ray Tune result with its `mlflow_run_id`, artifact URI, metrics, and selected hyperparameters.
- [apps/ml-system/src/training/ray_tune_train_bst.py line 247 (line 247)](../../../apps/ml-system/src/training/ray_tune_train_bst.py#L247): builds the best-trial payload containing the checkpoint path, artifact URI, MLflow run ID, dataset versions, and metrics.
- [apps/ml-system/src/registry/model_registry.py line 8 (line 8)](../../../apps/ml-system/src/registry/model_registry.py#L8): defines the helper that writes model metadata into the registry.
- [apps/ml-system/src/registry/model_registry.py line 25 (line 25)](../../../apps/ml-system/src/registry/model_registry.py#L25): creates the `model_configs` table with `model_version`, `artifact_uri`, `mlflow_run_id`, `metrics`, `config`, `serving_artifact_uri`, and `promotion_manifest_uri`.
- [apps/ml-system/src/registry/model_promotion.py line 471 (line 471)](../../../apps/ml-system/src/registry/model_promotion.py#L471): builds the promotion manifest with model version, source checkpoint, MLflow run ID, serving URI, and tensor schema.
- [apps/ml-system/src/registry/model_promotion.py line 510 (line 510)](../../../apps/ml-system/src/registry/model_promotion.py#L510): creates the MLflow registered model version through `MlflowClient.create_model_version`.
- [apps/ml-system/src/registry/model_promotion.py line 557 (line 557)](../../../apps/ml-system/src/registry/model_promotion.py#L557): promotes the selected checkpoint into a versioned Triton repository.
- [apps/ml-system/src/registry/model_promotion.py line 625 (line 625)](../../../apps/ml-system/src/registry/model_promotion.py#L625): writes promoted serving metadata back to the PostgreSQL model registry.

### Image proof

![MLflow model registry UI](../../pngs/mlflow_register_ui.png)

**Figure 1 - MLflow registered model UI.** Caption: the MLflow Model Registry shows `recsys_bst_ranker` with model-level tags (`model_family=bst`, `system=recsys-mlops`) and a concrete registered version. The version row carries tags such as `model_version`, `metric_name`, `metric_value`, and `source=kubeflow-ray-tune`, proving that the trained checkpoint is tracked as a versioned model artifact.

![Kubeflow model promotion UI](../../pngs/promote_model_log.png)

**Figure 2 - Kubeflow promotion manifest UI.** Caption: the Kubeflow Pipelines graph shows the `promote-bst-model` step completed successfully, and the log panel contains the promotion manifest fields (`model_name`, `model_version`, `mlflow_run_id`, `source_checkpoint_uri`, `triton_storage_uri`, `serving_storage_uri`, and `promotion_manifest_uri`). This proves that the chosen model version is packaged for Triton serving and linked back to the training lineage.

![Kubeflow model params UI](../../pngs/model_params_ui.png)

**Figure 3 - MLflow model parameters UI.** Caption: the MLflow training run stores flattened model/training configuration in the Parameters table. The screenshot shows model hyperparameters such as `model_args.n_heads`, `model_args.k_interests`, `model_args.embed_dim`, `model_args.seq_len`, `model_args.hidden_dropout_prob`, `model_args.attn_dropout_prob`, and `model_args.hidden_act`, which proves the hyperparameter side of **MODEL (weight, hyperparam)** versioning.

## Data Versioning

### Code reference

- [apps/ml-system/src/cli/prepare_bst_training_data.py line 448 (line 448)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L448): builds `dataset_version_meta.json` with dataset run ID, schema hash, feature source, row counts, table names, snapshot IDs, commit times, and tags.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 571 (line 571)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L571): enables dataset versioning when the Hudi versioning flag is active.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 573 (line 573)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L573): converts train/validation/test splits into versioned samples with `dataset_run_id`, feature service version, and processing code version.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 579 (line 579)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L579): commits the versioned samples into Hudi and returns commit metadata.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 604 (line 604)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L604): writes the final dataset version manifest file used by downstream training and MLflow lineage logging.
- [apps/ml-system/src/lineage/dataset_versioning.py line 146 (line 146)](../../../apps/ml-system/src/lineage/dataset_versioning.py#L146): creates stable `sample_id` values for deterministic sample identity.
- [apps/ml-system/src/lineage/dataset_versioning.py line 160 (line 160)](../../../apps/ml-system/src/lineage/dataset_versioning.py#L160): creates `row_hash` values to detect sample content changes.
- [apps/ml-system/src/lineage/dataset_versioning.py line 170 (line 170)](../../../apps/ml-system/src/lineage/dataset_versioning.py#L170): materializes each training sample with `sample_id`, `row_hash`, `dataset_run_id`, timestamps, feature service version, and processing code version.
- [apps/ml-system/src/lineage/dataset_versioning.py line 346 (line 346)](../../../apps/ml-system/src/lineage/dataset_versioning.py#L346): configures Hudi Copy-on-Write `upsert` with `sample_id` as the record key and `updated_at` as the precombine field.
- [apps/ml-system/src/lineage/dataset_versioning.py line 403 (line 403)](../../../apps/ml-system/src/lineage/dataset_versioning.py#L403): writes versioned training/evaluation samples to Hudi and captures table-level metadata.
- [apps/ml-system/src/lineage/dataset_versioning.py line 422 (line 422)](../../../apps/ml-system/src/lineage/dataset_versioning.py#L422): routes train/validation rows to the training table and test rows to the evaluation table.
- [apps/ml-system/src/lineage/dataset_versioning.py line 453 (line 453)](../../../apps/ml-system/src/lineage/dataset_versioning.py#L453): exports versioned Hudi rows back to JSONL files for model training.
- [apps/ml-system/src/lineage/mlflow_dataset_lineage.py line 34 (line 34)](../../../apps/ml-system/src/lineage/mlflow_dataset_lineage.py#L34): logs dataset run ID, schema hash, split row counts, table names, commit times, and tags to MLflow.
- [apps/ml-system/src/lineage/mlflow_dataset_lineage.py line 94 (line 94)](../../../apps/ml-system/src/lineage/mlflow_dataset_lineage.py#L94): stores the full `datasets/dataset_version_meta.json` artifact in MLflow.
- [infra/k8s/hudi-cli-data-versioning-proof.yaml line 1 (line 1)](../../../infra/k8s/hudi-cli-data-versioning-proof.yaml#L1): defines the reusable Hudi CLI proof pod that prints `desc`, `commits show`, and `show fsview all` for screenshot evidence.

### Apache Hudi incremental versioning flow

This project uses Apache Hudi for incremental dataset versioning. The flow is: Feast/PostgreSQL offline features are converted into BST train/validation/test samples, each sample gets a stable `sample_id` plus a `row_hash`, then Apache Hudi writes the samples with Copy-on-Write `upsert`. The Hudi record key is `sample_id`, the precombine field is `updated_at`, and the split column partitions the data. After each write, the pipeline records the Hudi table path, latest commit time, snapshot ID, split tag, and row count into `dataset_version_meta.json`; MLflow then logs the same metadata as parameters and as a durable artifact.

Hudi proof is captured with Hudi CLI by connecting directly to the Hudi table path and showing the active commit timeline. The CLI proof includes the table connection banner, `desc` output with `COPY_ON_WRITE`, `sample_id` record key, `updated_at` precombine field, and `split` partition field, plus `commits show` / `show fsview all` output showing commit instants and the versioned parquet file slices written by each incremental Hudi upsert.

**Proof pod note:** the Hudi CLI proof is now reproducible from the reusable Kubernetes manifest [infra/k8s/hudi-cli-data-versioning-proof.yaml line 1 (line 1)](../../../infra/k8s/hudi-cli-data-versioning-proof.yaml#L1). The manifest creates the fixed pod name `hudi-cli-data-versioning-proof` in namespace `recsys-dataflow`, mounts `recsys-data-platform-config` and `recsys-data-platform-secret`, connects to `s3a://recsys-offline-feature-store/warehouse/recsys_features/ml/bst_training_samples`, and prints `desc`, `commits show`, and `show fsview all` to pod logs. The pod is intentionally a one-shot `Pod` instead of a `Job`, so the screenshot command stays stable. To refresh and capture the proof again, run:

```bash
kubectl delete pod -n recsys-dataflow hudi-cli-data-versioning-proof --ignore-not-found
kubectl apply -f infra/k8s/hudi-cli-data-versioning-proof.yaml
kubectl logs -n recsys-dataflow hudi-cli-data-versioning-proof | less -S
```

In Hudi, a **file slice** is the concrete data-file version for a Hudi file group at a specific commit instant. For this Copy-on-Write table, each file slice points to a Parquet base file. When the same `FileId` appears across multiple `Base-Instant` values, it proves Hudi preserved incremental versions for the same logical file group instead of replacing the whole table.

### Image proof

![MLflow data version parameters](../../pngs/data_versioning_ui.png)

**Figure 4 - MLflow dataset version parameters.** Caption: the MLflow run page is filtered by `dataset` parameters and shows `dataset_run_id`, Hudi table names, Hudi commit times, split tags, row counts, JSONL paths, and versioning latency. This proves that the training run is tied to an exact Apache Hudi dataset snapshot.

![MLflow dataset version manifest artifact](../../pngs/dvc_artifacts.png)

**Figure 5 - MLflow dataset version manifest artifact.** Caption: the MLflow Artifacts tab opens `datasets/dataset_version_meta.json`, which persists the complete Apache Hudi lineage manifest: `storage=hudi`, catalog, warehouse path, train/validation/test row counts, Hudi table paths, commit times, snapshot IDs, and tags. This is the durable proof object that connects a model run to the exact incremental data version used for training and evaluation.

![Apache Hudi CLI data versioning proof 1](../../pngs/hudi_cli_1.png)

**Figure 6 - Hudi CLI table metadata and storage layout.** Caption: the Hudi CLI starts inside the proof pod, loads metadata for `bst_training_samples`, and prints the table `desc` output. The important fields are `basePath`, which points to the versioned training sample table in `s3a://recsys-offline-feature-store/warehouse/recsys_features/ml/bst_training_samples`; `metaPath`, which points to the `.hoodie` metadata directory; `fileSystem=s3a`, proving the table is stored in the MinIO/S3-compatible offline feature store; `hoodie.table.type=COPY_ON_WRITE`, proving Hudi stores committed parquet versions; and `hoodie.table.precombine.field=updated_at`, proving Hudi resolves repeated upserts for the same sample by the latest update timestamp.

![Apache Hudi CLI data versioning proof 2](../../pngs/hudi_cli_2.png)

**Figure 7 - Hudi active timeline.** Caption: the Hudi CLI timeline lists completed `commit` instants from `20260630150403897` through `20260701190855003`. Each `COMPLETED` row is one successful dataset version written to the active Hudi timeline, with requested, inflight, and completed timestamps proving that each version finished cleanly.

![Apache Hudi CLI data versioning proof 3](../../pngs/hudi_cli_3.png)

**Figure 8 - Hudi commit write stats and file slices.** Caption: the upper table is `commits show`: each `CommitTime` records one dataset version, `Total Bytes Written` is about `1.5 MB`, `Total Partitions Written=2` proves both `split=train` and `split=val` were written, and `Total Records Written=3503` proves the exact training/validation sample count in each version. The first commit adds two files, while later commits update two files and write `3503` update records, showing incremental upsert behavior. The lower `show fsview all` table maps partitions and `FileId`s to `Base-Instant` values and parquet `Data-File` paths.

**Where incremental versioning is shown in Figure 8:** incremental versioning is shown in two places. First, in the `commits show` table, the initial commit `20260630150403897` has `Total Files Added=2` and `Total Files Updated=0`, while later commits have `Total Files Added=0`, `Total Files Updated=2`, and `Total Update Records Written=3503`. This means later dataset versions update existing Hudi file groups instead of creating a full new table copy. Second, in the `show fsview all` table, the same `FileId` appears multiple times with different `Base-Instant` values and different parquet `Data-File` paths. That is the storage-level proof that Hudi keeps incremental versions over time.

**File slice explanation for Figure 8:** a Hudi file slice is one physical data-file version inside a Hudi file group at one commit instant. In this proof, the same `FileId` appears repeatedly for `split=val` and `split=train`, but each row has a different `Base-Instant`. That means Hudi kept multiple incremental versions of the same logical file group instead of replacing the whole table. The `Data-File` path also embeds the commit instant, so each parquet file can be traced back to the exact dataset version that produced it.
