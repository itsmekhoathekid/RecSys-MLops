# ML Pipelines

## Training Pipeline

### Tech stacks

- **Kubeflow Pipelines (KFP):** orchestrates the ML workflow as containerized steps.
- **Feast PostgreSQL offline store:** `prepare-training-data` reads entity/label rows from `feature_store.ml_ranking_labels`, then uses Feast native `FeatureStore.get_historical_features(...)` with FeatureService `bst_ranking_v1`.
- **Feast Redis online store:** online serving stays separate from the training pipeline; the training path uses point-in-time historical retrieval from PostgreSQL.
- **Spark submit image:** runs the data-prep CLI inside the KFP step. Feast's core offline store for this coursework scope is PostgreSQL; extra Spark packages in the image are not part of the Feast store.
- **PyTorch BST model:** trains the existing `BST` recommender model using `recommenderDataset` and `Trainer`.
- **KubeRay RayJob + Ray Tune:** the KFP train step submits a RayJob with Ray head/worker pods and runs distributed hyperparameter trials.
- **MLflow + MinIO + Postgres model registry:** stores metrics, checkpoints, artifacts, model registry versions, and promoted model metadata.
- **Triton-compatible promotion:** exports the best BST checkpoint into a Triton serving layout and writes a promotion manifest.

Notebook-to-pipeline mapping:

| Notebook step | Pipeline step | What it does |
| --- | --- | --- |
| Load labels and historical features through Feast | `prepare-training-data` | Reads PostgreSQL entity/label rows from `feature_store.ml_ranking_labels`, retrieves point-in-time features through Feast `bst_ranking_v1`, and prepares BST rows. |
| Split train/validation/test data | `prepare-training-data` | Writes `train.jsonl`, `val.jsonl`, `test.jsonl`, and dataset metadata under the configured split output directory. |
| Train model | `submit-rayjob` | Submits KubeRay `RayJob` `recsys-bst-ray-tune`, then runs Ray Tune trials for BST training. |
| Evaluate model | `evaluate-bst` | Evaluates the best Ray result on the test split and logs evaluation metrics. |
| Save/promote model | `promote-bst-model` | Exports the best checkpoint, writes Triton serving files, updates model metadata, and writes the promotion manifest. |

### Code reference

- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 223 (line 223)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L223): defines the KFP pipeline `recsys_bst_pipeline`.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 44 (line 44)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L44): `prepare_training_data` component runs the data-prep CLI through Spark submit.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 232 (line 232)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L232): pipeline default entity input is PostgreSQL `feature_store.ml_ranking_labels`.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 248 (line 248)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L248): pipeline default FeatureService is `bst_ranking_v1`.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 267 (line 267)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L267): wires `prepare-training-data`.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 287 (line 287)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L287): wires `submit-rayjob`.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 314 (line 314)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L314): wires `evaluate-bst`.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 326 (line 326)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L326): wires `promote-bst-model`.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 149 (line 149)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L149): reads `postgresql://...` entity/label input.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 344 (line 344)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L344): calls Feast `FeatureStore.get_historical_features(...)`.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 509 (line 509)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L509): writes BST JSONL train/validation/test splits.
- [apps/ml-system/src/cli/submit_ray_job.py line 33 (line 33)](../../../apps/ml-system/src/cli/submit_ray_job.py#L33): builds the KubeRay RayJob spec with runtime secret and shared PVC.
- [apps/ml-system/src/training/ray_tune_train_bst.py line 126 (line 126)](../../../apps/ml-system/src/training/ray_tune_train_bst.py#L126): runs each Ray Tune BST trial.
- [apps/ml-system/src/training/train.py line 25 (line 25)](../../../apps/ml-system/src/training/train.py#L25): logs params, metrics, checkpoint, and lineage to MLflow.
- [apps/ml-system/src/training/train.py line 73 (line 73)](../../../apps/ml-system/src/training/train.py#L73): shared BST training entrypoint used by Ray trials.
- [apps/ml-system/src/cli/evaluate_bst.py line 34 (line 34)](../../../apps/ml-system/src/cli/evaluate_bst.py#L34): evaluates a trained BST checkpoint on the selected split.
- [apps/ml-system/src/registry/model_promotion.py line 557 (line 557)](../../../apps/ml-system/src/registry/model_promotion.py#L557): promotes the best checkpoint to Triton/MinIO/model registry metadata.
- [apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py line 26 (line 26)](../../../apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py#L26): compiles the KFP package to `infra/kubeflow/compiled/bst_training_pipeline.yaml`.
- [apps/ml-system/src/kubeflow/submit_pipeline_run.py line 116 (line 116)](../../../apps/ml-system/src/kubeflow/submit_pipeline_run.py#L116): submits and optionally waits for the KFP run.
- [apps/ml-system/src/kubeflow/components/runtime.py line 37 (line 37)](../../../apps/ml-system/src/kubeflow/components/runtime.py#L37): mounts the shared PVC and runtime secret into KFP tasks.

### Compiled pipeline defaults

| Parameter | Current value |
| --- | --- |
| Pipeline name | `recsys-bst-feature-train-evaluate` |
| Entity input path | `postgresql://feature-postgres.recsys-dataflow.svc.cluster.local:5432/feature_store/feature_store.ml_ranking_labels` |
| Feature service | `bst_ranking_v1` |
| Split output | `/workspace/recsys/data_platform/output/ml/bst_split` |
| Dataset metadata | `/workspace/recsys/data_platform/output/ml/bst_split/dataset_version_meta.json` |
| Ray job name | `recsys-bst-ray-tune` |
| Ray namespace | `kubeflow` |
| Ray output | `/workspace/recsys/data_platform/output/ml/ray` |
| Evaluation metrics | `/workspace/recsys/data_platform/output/ml/eval_metrics.json` |
| Promotion manifest | `/workspace/recsys/data_platform/output/ml/serving/promotion_manifest.json` |
| Promotion metric | `test_ndcg_at_10` |

### Description

- `prepare-training-data` proves historical feature retrieval: it reads labels from PostgreSQL `feature_store.ml_ranking_labels`, calls Feast FeatureService `bst_ranking_v1`, then writes BST JSONL splits and dataset metadata.
- `submit-rayjob` proves distributed training: it creates KubeRay `RayJob` `recsys-bst-ray-tune` with one Ray head pod and configured worker pods, then runs Ray Tune trials.
- RayJob status should end with `jobStatus: SUCCEEDED`; the best result is written to `/workspace/recsys/data_platform/output/ml/ray/best_result.json`.
- `evaluate-bst` writes metrics to `/workspace/recsys/data_platform/output/ml/eval_metrics.json`.
- `promote-bst-model` writes the serving/promotion manifest to `/workspace/recsys/data_platform/output/ml/serving/promotion_manifest.json`.

### Image proof of Kubeflow pipeline preparing training data log

![Data & ML system](../../pngs/prep_training_data_log.png)

### Image proof of Kubeflow pipeline submit RayJob log

![Data & ML system](../../pngs/submit_rayjob_log.png)

### Image proof of Kubeflow pipeline model evaluation log

![Data & ML system](../../pngs/evaluate_model_log.png)

### Image proof of Kubeflow pipeline model promotion

![Data & ML system](../../pngs/promote_model_log.png)

### Image proof of distributed Ray cluster training

![Data & ML system](../../pngs/ray_tune_running.png)

Comments:

- `recsys-bst-ray-tune` is the KubeRay `RayJob` submitted by the Kubeflow `submit-rayjob` step. This is the distributed training job, not a local notebook run.
- `recsys-bst-ray-tune-<suffix>` is the Ray cluster created for that RayJob.
- `recsys-bst-ray-tune-<suffix>-head-...` is the Ray head pod. It coordinates the cluster, receives the Ray job submission, and schedules Ray Tune training tasks.
- `recsys-bst-ray-tune-<suffix>-cpu-or-gpu-workers-worker-...` is the Ray worker pod. It joins the Ray cluster and executes training tasks/trials assigned by the head pod.
- `recsys-bst-ray-tune-...` submitter pod runs the Ray job entrypoint and waits for the distributed RayJob status.
- Seeing the Ray head pod, worker pod, and submitter pod running together proves that training is executed through a distributed Ray cluster instead of a single local process.

### Image proof of RayJob successful run

![Data & ML system](../../pngs/ray_cluster_ui.png)
