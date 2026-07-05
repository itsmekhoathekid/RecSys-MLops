# ML Pipelines

## Training Pipeline

### Tech stacks

- **Kubeflow Pipelines (KFP):** orchestrates the ML workflow as containerized steps.
- **Feast PostgreSQL offline store:** `prepare-training-data` reads entity/label rows from `feature_store.ml_ranking_labels`, then uses Feast native `FeatureStore.get_historical_features(...)` with FeatureService `bst_ranking_v1`.
- **Feast Redis online store:** online serving stays separate from the training pipeline; the training path uses point-in-time historical retrieval from PostgreSQL.
- **Spark submit image:** runs the data-prep CLI inside the KFP step. Feast's core offline store for this coursework scope is PostgreSQL; extra Spark packages in the image are not part of the Feast store.
- **PyTorch BST model:** trains the existing `BST` recommender model using `recommenderDataset` and `Trainer`.
- **KubeRay RayJob + Ray Tune:** the first Ray step runs small, fast hyperparameter tuning trials (`1` epoch, `1%` training data by default) and writes `tune_result.json`.
- **KubeRay RayJob + Ray Train DDP:** the second Ray step consumes the best tune config and runs real distributed PyTorch training (`DistributedDataParallel`) with small defaults (`1` epoch, `2%` training data, `2` workers) so proof runs quickly.
- **MLflow + MinIO + Postgres model registry:** stores metrics, checkpoints, artifacts, model registry versions, and promoted model metadata.
- **Triton-compatible promotion:** exports the best BST checkpoint into a Triton serving layout and writes a promotion manifest.

Notebook-to-pipeline mapping:

| Notebook step | Pipeline step | What it does |
| --- | --- | --- |
| Load labels and historical features through Feast | `prepare-training-data` | Reads PostgreSQL entity/label rows from `feature_store.ml_ranking_labels`, retrieves point-in-time features through Feast `bst_ranking_v1`, and prepares BST rows. |
| Split train/validation/test data | `prepare-training-data` | Writes `train.jsonl`, `val.jsonl`, `test.jsonl`, and dataset metadata under the configured split output directory. |
| Tune hyperparameters | `Hyperparameter tuning` (`submit-rayjob`, `job_mode=tune`) | Submits KubeRay `RayJob` `recsys-bst-ray-tune`, then runs small Ray Tune trials and writes `tune_result.json`. |
| Distributed train model | `Distributed training` (`submit-rayjob-2`, `job_mode=distributed-train`) | Submits KubeRay `RayJob` `recsys-bst-ray-ddp-train`, then runs Ray Train `TorchTrainer` with PyTorch DDP, rank-aware data sharding, gradient synchronization, checkpointing, and Ray reporting. |
| Evaluate model | `evaluate-bst` | Evaluates the best Ray result on the test split and logs evaluation metrics. |
| Save/promote model | `promote-bst-model` | Exports the best checkpoint, writes Triton serving files, updates model metadata, and writes the promotion manifest. |
| Deploy promoted model | `Trigger KServe CD` | Checks the promotion metric against the deployment threshold, then triggers Jenkins job `RecSys-KServe-Model-CD` to deploy the promoted Triton repository to KServe. |

### Code reference

- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 232 (line 232)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L232): defines the KFP pipeline `recsys_bst_pipeline`.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 44 (line 44)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L44): `prepare_training_data` component runs the data-prep CLI through Spark submit.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 241 (line 241)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L241): pipeline default entity input is PostgreSQL `feature_store.ml_ranking_labels`.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 260 (line 260)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L260): pipeline default FeatureService is `bst_ranking_v1`.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 302 (line 302)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L302): wires the Ray Tune step before distributed training.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 333 (line 333)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L333): wires the DDP distributed training step after Ray Tune.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 364 (line 364)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L364): wires `evaluate-bst` after DDP training.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 375 (line 375)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L375): wires `promote-bst-model`.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 149 (line 149)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L149): reads `postgresql://...` entity/label input.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 344 (line 344)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L344): calls Feast `FeatureStore.get_historical_features(...)`.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 509 (line 509)](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L509): writes BST JSONL train/validation/test splits.
- [apps/ml-system/src/cli/submit_ray_job.py line 95 (line 95)](../../../apps/ml-system/src/cli/submit_ray_job.py#L95): switches RayJob entrypoint between `tune` and `distributed-train`.
- [apps/ml-system/src/cli/submit_ray_job.py line 152 (line 152)](../../../apps/ml-system/src/cli/submit_ray_job.py#L152): builds the KubeRay RayJob spec with runtime secret, shared PVC, head pod, and worker pods.
- [apps/ml-system/src/training/ray_tune_train_bst.py line 126 (line 126)](../../../apps/ml-system/src/training/ray_tune_train_bst.py#L126): runs each Ray Tune BST trial.
- [apps/ml-system/src/training/ray_tune_train_bst.py line 197 (line 197)](../../../apps/ml-system/src/training/ray_tune_train_bst.py#L197): keeps Ray Tune small by default (`1` epoch, `2` trials, serial concurrency unless overridden).
- [apps/ml-system/src/training/ray_distributed_train_bst.py line 214 (line 214)](../../../apps/ml-system/src/training/ray_distributed_train_bst.py#L214): Ray Train worker loop initializes rank/world size and prepares the DDP model.
- [apps/ml-system/src/training/ray_distributed_train_bst.py line 236 (line 236)](../../../apps/ml-system/src/training/ray_distributed_train_bst.py#L236): uses `DistributedSampler` so each rank receives its own shard of training data.
- [apps/ml-system/src/training/ray_distributed_train_bst.py line 261 (line 261)](../../../apps/ml-system/src/training/ray_distributed_train_bst.py#L261): runs `loss.backward()` through the DDP-wrapped model, which synchronizes gradients across ranks.
- [apps/ml-system/src/training/ray_distributed_train_bst.py line 287 (line 287)](../../../apps/ml-system/src/training/ray_distributed_train_bst.py#L287): broadcasts rank-0 validation metrics back to all workers before Ray reporting.
- [apps/ml-system/src/training/ray_distributed_train_bst.py line 301 (line 301)](../../../apps/ml-system/src/training/ray_distributed_train_bst.py#L301): reports metrics/checkpoints through Ray Train.
- [apps/ml-system/src/training/ray_distributed_train_bst.py line 303 (line 303)](../../../apps/ml-system/src/training/ray_distributed_train_bst.py#L303): rank 0 logs the final DDP checkpoint, metrics, and dataset lineage to MLflow/model registry.
- [apps/ml-system/src/cli/evaluate_bst.py line 34 (line 34)](../../../apps/ml-system/src/cli/evaluate_bst.py#L34): evaluates a trained BST checkpoint on the selected split.
- [apps/ml-system/src/registry/model_promotion.py line 557 (line 557)](../../../apps/ml-system/src/registry/model_promotion.py#L557): promotes the best checkpoint to Triton/MinIO/model registry metadata.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 238 (line 238)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L238): defines the `Trigger KServe CD` container component.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 437 (line 437)](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L437): wires `Trigger KServe CD` after `promote-bst-model`.
- [apps/ml-system/src/cli/trigger_kserve_cd.py line 188 (line 188)](../../../apps/ml-system/src/cli/trigger_kserve_cd.py#L188): loads the promotion manifest, checks the score threshold, and triggers Jenkins.
- [jenkins/KServeModelCD.Jenkinsfile line 1 (line 1)](../../../jenkins/KServeModelCD.Jenkinsfile#L1): Jenkins job that deploys the promoted model after training/retraining.
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
| Ray tune job name | `recsys-bst-ray-tune` |
| Ray DDP train job name | `recsys-bst-ray-ddp-train` |
| Ray namespace | `kubeflow` |
| Ray output | `/workspace/recsys/data_platform/output/ml/ray` |
| Ray tune output | `/workspace/recsys/data_platform/output/ml/ray/tune_result.json` |
| Ray final DDP output | `/workspace/recsys/data_platform/output/ml/ray/best_result.json` |
| Tune defaults | `training_percent=0.01`, `num_epochs=1`, `max_trials=2`, `parallel_trials=1` |
| DDP defaults | `distributed_training_percent=0.02`, `distributed_num_epochs=1`, `distributed_num_workers=2` |
| Evaluation metrics | `/workspace/recsys/data_platform/output/ml/eval_metrics.json` |
| Promotion manifest | `/workspace/recsys/data_platform/output/ml/serving/promotion_manifest.json` |
| Promotion metric | `test_ndcg_at_10` |
| KServe CD threshold | `0.0` |
| KServe CD Jenkins job | `RecSys-KServe-Model-CD` |
| KServe CD status | `/workspace/recsys/data_platform/output/ml/serving/kserve_cd_status.json` |

### Description

- `prepare-training-data` proves historical feature retrieval: it reads labels from PostgreSQL `feature_store.ml_ranking_labels`, calls Feast FeatureService `bst_ranking_v1`, then writes BST JSONL splits and dataset metadata.
- `Hyperparameter tuning` is the Kubeflow UI display name for internal task `submit-rayjob` with `job_mode=tune`: it creates KubeRay `RayJob` `recsys-bst-ray-tune`, runs small Ray Tune trials, and writes `/workspace/recsys/data_platform/output/ml/ray/tune_result.json`.
- `Distributed training` is the Kubeflow UI display name for internal task `submit-rayjob-2` with `job_mode=distributed-train`: it creates KubeRay `RayJob` `recsys-bst-ray-ddp-train`, consumes `tune_result.json`, and writes the final DDP result to `/workspace/recsys/data_platform/output/ml/ray/best_result.json`.
- The DDP training step is the real distributed training proof: `TorchTrainer` creates multiple workers, PyTorch DDP syncs gradients during `loss.backward()`, `DistributedSampler` shards data by rank, rank 0 saves the checkpoint, and all workers call Ray Train report with synced metrics.
- Both RayJobs should end with `jobStatus: SUCCEEDED`.
- `evaluate-bst` writes metrics to `/workspace/recsys/data_platform/output/ml/eval_metrics.json`.
- `promote-bst-model` writes the serving/promotion manifest to `/workspace/recsys/data_platform/output/ml/serving/promotion_manifest.json`.
- `Trigger KServe CD` is the post-training/post-retraining deployment handoff: it reads the promotion manifest, verifies the metric threshold, triggers Jenkins `RecSys-KServe-Model-CD`, waits for the Jenkins build, and writes `/workspace/recsys/data_platform/output/ml/serving/kserve_cd_status.json`.

### Image proof of Kubeflow pipeline preparing training data log

![Data & ML system](../../pngs/prep_training_data_log.png)

**Figure: Kubeflow `prepare-training-data` log.** This proof shows the pipeline loading label/entity rows from the PostgreSQL Feast offline store, retrieving historical features through Feast, writing BST train/validation/test JSONL splits, and recording dataset lineage/version metadata before any model training starts.

### Image proof of Kubeflow pipeline submit Ray Tune and DDP RayJob logs

![Data & ML system](../../pngs/hyperparam_tuning_ray_log.png)

**Figure: Kubeflow `Hyperparameter tuning` log.** This proof shows the first KubeRay job submission for Ray Tune. The step runs small tuning trials, selects the best hyperparameter config, writes `tune_result.json`, and passes that result to the next training step.

![Data & ML system](../../pngs/ddp_training_log.png)

**Figure: Kubeflow `Distributed training` log.** This proof shows the second KubeRay job submission for Ray Train DDP. The important evidence is that this stage consumes the tuned config, starts a Ray Train worker group, runs the DDP training entrypoint, and reports `SUCCEEDED` only after distributed training finishes.

### Image proof of Kubeflow pipeline model evaluation log

![Data & ML system](../../pngs/evaluate_model_log.png)

**Figure: Kubeflow `evaluate-bst` log.** This proof shows the promoted candidate checkpoint being evaluated on the held-out test split. The log records test metrics such as loss, AUC, hit-rate, MRR, and NDCG, then writes the evaluation result used by the promotion step.

### Image proof of Kubeflow pipeline model promotion

![Data & ML system](../../pngs/promote_model_log.png)

**Figure: Kubeflow `promote-bst-model` log.** This proof shows the selected checkpoint being exported to the Triton serving layout, registered in MLflow, uploaded to the model store, and written into the promotion manifest. The `source` field should identify the model as coming from the DDP training stage.

Pipeline proof comments:

- `recsys-bst-ray-tune` is the first KubeRay `RayJob`; it runs fast Ray Tune trials only.
- `recsys-bst-ray-ddp-train` is the second KubeRay `RayJob`; it runs final distributed DDP training with the tuned config.
- `recsys-bst-ray-tune-<suffix>` is the Ray cluster created for that RayJob.
- `recsys-bst-ray-tune-<suffix>-head-...` is the Ray head pod. It coordinates the cluster, receives the Ray job submission, and schedules Ray Tune training tasks.
- `recsys-bst-ray-ddp-train-<suffix>-cpu-or-gpu-workers-worker-...` is the worker pod set used by Ray Train; each worker maps to a DDP rank.
- `recsys-bst-ray-tune-...` and `recsys-bst-ray-ddp-train-...` submitter pods run the Ray job entrypoints and wait for each RayJob status.
- Seeing the Ray head pod, worker pods, and submitter pod running together proves that the tune and DDP training stages execute through KubeRay rather than a single local process.

### Image proof of Ray Dashboard DDP distributed training

![ddp_training_ui_1](../../pngs/ddp_training_ui_1.png)

**Figure: Ray Dashboard DDP job proof.** This screenshot should show the distributed training Ray job, not the tuning job. The proof points are the DDP entrypoint `ray_distributed_train_bst.py`, the KubeRay job status, and Ray job logs that include `Started training worker group of size 2`, `DistributedDataParallel`, `world_size=2`, `rank=0`, `rank=1`, `distributed_sampler=True`, and `ddp_gradient_sync=True`.

![ddp_training_ui_2](../../pngs/ddp_training_ui_2.png)

**Figure: Ray Dashboard DDP cluster proof.** This screenshot should show the Ray cluster resources used by distributed training. The expected evidence is one Ray head plus two Ray workers, matching the DDP worker count and proving that training is not a single local process.

![ddp training k9s](../../pngs/ddp_training_k9s.png)

**Figure: k9s Ray Tune and DDP pod proof.** This screenshot shows the Kubernetes side of both Ray stages in the training flow. The `recsys-bst-ray-tune-...` pods are the hyperparameter tuning RayJob: a Ray head pod, Ray worker pod, and completed submitter pod. The `recsys-bst-ray-ddp-train-...` pods are the final distributed training RayJob: a Ray head pod, two Ray worker pods, and completed submitter pod. Seeing both groups proves that Kubeflow launched separate KubeRay jobs for tuning and DDP training, and that the DDP stage used multiple Ray workers instead of a single local process.
