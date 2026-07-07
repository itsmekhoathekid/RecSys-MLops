# Repository Design Proof

This proof covers the final-coursework rubric item **Repository Design: clean code, clean repo, and demonstrated design pattern usage**.

## Rubric Mapping

| Rubric requirement | Repository evidence |
|---|---|
| Clean repo | Source code is split by bounded context: API serving, data platform, ML system, infrastructure, tests, and submission docs. |
| Clean code | Runtime logic is decomposed into schema, routing, feature access, ranking, observability, orchestration, model training, and promotion modules. |
| Design pattern usage | The codebase uses Strategy/Router, Adapter/Gateway, Protocol/Dependency Injection, Service Layer/Facade, Template Method/Lifecycle Service, Composite, Builder/Manifest, and Pipeline/Chain patterns. |
| Proof to capture | Screenshots of folder layout, tests, and code snippets where the design patterns are implemented. |

## Clean Repository Layout

The repository is organized around deployable and testable service boundaries instead of one large application folder.

```text
apps/
  api-serving/                 FastAPI recommendation API, online feature API, A/B router, Triton client
  data-platform/               Airflow, Spark, Flink, Feast feature store, ingestion, validation
  ml-system/                   BST model, Kubeflow pipeline, Ray Tune/DDP training, MLflow, promotion
infra/
  helm/                        Helm charts for serving, data platform, observability, security, gateway, CI
  terraform/gcp/               GCP/GKE infrastructure as code
  kubeflow/                    Compiled Kubeflow pipeline packages
jenkins/
  scripts/                     Shared path-based CI/CD scripts
tests/
  unit/                        Fast isolated tests by component
  contract/                    Manifest/chart/pipeline contract tests
  integration/                 Cross-service integration tests
  e2e/                         Live system verification tests
  load/                        Locust load-test scenarios
docs/
  submission/                  Rubric proof documents
  pngs/                        UI and terminal proof screenshots
```

The GCP Terraform layout follows the same separation of concerns.

| Terraform file | Responsibility |
|---|---|
| `apis.tf` | Required GCP APIs. |
| `network.tf` | VPC, subnet, secondary IP ranges. |
| `gke.tf` | GKE cluster and node pools. |
| `registry_storage.tf` | Artifact Registry and model/data storage buckets. |
| `cloudbuild.tf` | Cloud Build permissions and build integration. |
| `namespaces.tf` | Kubernetes namespaces and mesh injection labels. |
| `dependencies.tf` | Shared operators: cert-manager, KEDA, KServe, Istio, External Secrets. |
| `recsys_services.tf` | RecSys Helm releases. |
| `secret_management.tf` | Central source secrets for External Secrets Operator. |

### Code Reference

- [README.md line 42](../../../README.md#L42): top-level repository structure and navigation.
- [infra/terraform/gcp/gke.tf line 1](../../../infra/terraform/gcp/gke.tf#L1): GKE cluster and node-pool infrastructure boundary.
- [infra/helm/recsys-serving/templates/api-deployment.yaml line 1](../../../infra/helm/recsys-serving/templates/api-deployment.yaml#L1): API serving deployment boundary.
- [infra/helm/recsys-data-platform/templates/airflow.yaml line 1](../../../infra/helm/recsys-data-platform/templates/airflow.yaml#L1): data platform orchestration boundary.
- [infra/helm/recsys-security/templates/istio-authorization.yaml line 1](../../../infra/helm/recsys-security/templates/istio-authorization.yaml#L1): security policy boundary.

### Image Proof

**Capture command**

```bash
find apps infra/helm infra/terraform/gcp jenkins tests docs/submission -maxdepth 2 -type d | sort | sed -n '1,140p'
```

![Clean repository folder boundary proof](../../pngs/clean_repo_evidence.png)

**Figure: Clean repository folder boundary proof.** This screenshot should show the high-level source tree split by ownership boundary. The important proof is that API serving, data platform, ML system, infrastructure, CI/CD, tests, and submission docs are separate directories instead of mixed together.

## Clean Code Boundaries

The code keeps each runtime responsibility in a focused module:

| Boundary | Main files | Responsibility |
|---|---|---|
| API schema | [apps/api-serving/src/api_schemas.py line 1](../../../apps/api-serving/src/api_schemas.py#L1) | Pydantic request/response contracts. |
| A/B routing | [apps/api-serving/src/ab_testing.py line 20](../../../apps/api-serving/src/ab_testing.py#L20) | Control/candidate routing and experiment labels. |
| Feature access | [apps/api-serving/src/online_features.py line 82](../../../apps/api-serving/src/online_features.py#L82) | Feast/Redis online feature access behind one client. |
| Ranking orchestration | [apps/api-serving/src/ranking.py line 122](../../../apps/api-serving/src/ranking.py#L122) | Recommendation flow: pull features, route ranker, build payload, format response. |
| Triton gateway | [apps/api-serving/src/triton.py line 18](../../../apps/api-serving/src/triton.py#L18) | Triton gRPC inference client. |
| Training data service | [apps/ml-system/src/cli/prepare_bst_training_data.py line 391](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L391) | Feast/offline-store training table loading, schema validation, and canonical BST dataframe construction. |
| Temporal split service | [apps/ml-system/src/cli/prepare_bst_training_data.py line 574](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L574) | Time-ordered train/validation/test split creation, JSONL writing, and dataset-version metadata. |
| Ray Tune training loop | [apps/ml-system/src/models/trainer.py line 58](../../../apps/ml-system/src/models/trainer.py#L58) | Single-node BST trial training/evaluation lifecycle used by Ray Tune. |
| Ray DDP lifecycle | [apps/ml-system/src/training/ray_distributed_train_bst.py line 104](../../../apps/ml-system/src/training/ray_distributed_train_bst.py#L104) | Distributed BST training lifecycle for Ray Train DDP workers. |
| Model promotion | [apps/ml-system/src/registry/model_promotion.py line 559](../../../apps/ml-system/src/registry/model_promotion.py#L559) | Export, register, upload, and manifest generation. |
| Data generation pipeline | [apps/data-platform/data-generator/src/pipeline.py line 21](../../../apps/data-platform/data-generator/src/pipeline.py#L21) | Simulation, challenge injection, validation, sink writing, manifest output. |

## Design Patterns In Code

### Pattern 1: Strategy / Router For A/B Inference

**Intent:** choose one of several interchangeable model-serving strategies at runtime without changing the recommendation flow.

**External reference:** [Strategy pattern](https://en.wikipedia.org/wiki/Strategy_pattern).

**Implementation:** `TritonABRouter` owns the control/candidate route selection. `recommend()` only asks `select_triton_route()` for the route, then calls the selected ranker. The routing decision is isolated from payload building and response formatting.

| Code reference | What to point out in the screenshot |
|---|---|
| [apps/api-serving/src/ab_testing.py line 20](../../../apps/api-serving/src/ab_testing.py#L20) | `TritonABRouter` encapsulates A/B routing state. |
| [apps/api-serving/src/ab_testing.py line 71](../../../apps/api-serving/src/ab_testing.py#L71) | `assign()` maps a user deterministically to control/candidate. |
| [apps/api-serving/src/ab_testing.py line 80](../../../apps/api-serving/src/ab_testing.py#L80) | `route()` returns a `TritonRoute` with ranker, variant, experiment, and model version. |
| [apps/api-serving/src/ranking.py line 128](../../../apps/api-serving/src/ranking.py#L128) | `recommend()` delegates model choice to `select_triton_route()`. |

![Strategy router design pattern proof](../../pngs/repo_design_pattern_strategy_router.png)

**Figure: Strategy/Router design pattern proof.** Capture `TritonABRouter.assign()`, `TritonABRouter.route()`, and the `recommend()` call to `select_triton_route()`. This proves model routing is a replaceable strategy instead of hard-coded `if candidate then call service B` logic inside the ranking flow.

### Pattern 2: Adapter / Gateway For Online Feature Store Access

**Intent:** hide storage-specific details behind a small domain client so API code does not depend directly on Redis/Feast calls everywhere.

**External reference:** [Adapter pattern](https://en.wikipedia.org/wiki/Adapter_pattern).

**Implementation:** `FeatureClient` adapts Feast online retrieval and Redis configuration into domain methods such as `user_sequence()` and `item_features_batch()`. The ranking flow depends on feature operations, not low-level storage commands.

| Code reference | What to point out in the screenshot |
|---|---|
| [apps/api-serving/src/online_features.py line 82](../../../apps/api-serving/src/online_features.py#L82) | `FeatureClient` is the adapter boundary. |
| [apps/api-serving/src/online_features.py line 122](../../../apps/api-serving/src/online_features.py#L122) | Lazy construction of Feast `FeatureStore`. |
| [apps/api-serving/src/online_features.py line 139](../../../apps/api-serving/src/online_features.py#L139) | Domain method for user sequence features. |
| [apps/api-serving/src/online_features.py line 160](../../../apps/api-serving/src/online_features.py#L160) | Domain method for batch item features. |

![Adapter gateway design pattern proof](../../pngs/repo_design_pattern_feature_adapter.png)

**Figure: Adapter/Gateway design pattern proof.** Capture `FeatureClient` and one of its domain methods. This proves Redis/Feast details are localized in one gateway class while the serving code consumes a clean feature API.

### Pattern 3: Protocol + Dependency Injection For Ranker Substitution

**Intent:** allow production Triton ranker and test/deterministic rankers to share the same interface.

**External references:** [Python Protocol / structural subtyping](https://docs.python.org/3/library/typing.html#typing.Protocol), [PEP 544: Protocols](https://peps.python.org/pep-0544/), and [Dependency Injection](https://en.wikipedia.org/wiki/Dependency_injection).

**Implementation:** `RankerProtocol` defines the expected `score()` method. `TritonRanker` implements that protocol for production, and tests can inject fake rankers without starting Triton.

| Code reference | What to point out in the screenshot |
|---|---|
| [apps/api-serving/src/triton.py line 13](../../../apps/api-serving/src/triton.py#L13) | `RankerProtocol` defines the ranker interface. |
| [apps/api-serving/src/triton.py line 18](../../../apps/api-serving/src/triton.py#L18) | `TritonRanker` implements production gRPC inference. |
| [apps/api-serving/src/ranking.py line 122](../../../apps/api-serving/src/ranking.py#L122) | `recommend()` receives a `RankerProtocol` or `TritonABRouter` dependency. |
| [tests/unit/api_serving/test_split_services.py line 22](../../../tests/unit/api_serving/test_split_services.py#L22) | Unit tests inject deterministic rankers. |

![Protocol dependency injection proof](../../pngs/repo_design_pattern_ranker_protocol.png)

**Figure: Protocol/Dependency Injection design pattern proof.** Capture `RankerProtocol`, `TritonRanker`, and a fake/deterministic test ranker. This proves the ranking flow is testable because the model-serving dependency can be replaced.

### Pattern 4: Service Layer / Facade For ML Training Data Preparation

**Intent:** keep feature-source access, schema validation, temporal splitting, and dataset-version writing behind focused service boundaries.

**External reference:** [Facade pattern](https://en.wikipedia.org/wiki/Facade_pattern).

**Implementation:** `TrainingDataService` hides Feast and offline feature store loading behind one training-table API. `SplitService` hides temporal sorting, row normalization, JSONL split writing, and dataset metadata writing. The KFP `prepare-training-data` component calls the data-prep flow, but the flow delegates source-specific IO and split policy to these classes.

| Code reference | What to point out in the screenshot |
|---|---|
| [apps/ml-system/src/cli/prepare_bst_training_data.py line 391](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L391) | `TrainingDataService` class with `read_training_table()`, Feast loading, offline-store loading, schema validation, and canonical frame building. |
| [apps/ml-system/src/cli/prepare_bst_training_data.py line 574](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L574) | `SplitService` class with temporal sort, row normalization, split boundaries, JSONL output, and dataset metadata. |
| [apps/ml-system/src/cli/prepare_bst_training_data.py line 647](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L647) | `prepare_bst_jsonl_splits()` wires both services into the actual pipeline flow. |
| [tests/unit/ml_system/test_prepare_bst_training_data.py line 1](../../../tests/unit/ml_system/test_prepare_bst_training_data.py#L1) | Unit tests prove the service boundary validates schema and creates temporal splits. |

![Training data service facade proof](../../pngs/repo_design_pattern_training_data_service.png)

**Figure: TrainingDataService facade proof.** Capture `TrainingDataService` and the call site in `prepare_bst_jsonl_splits()`. This proves the ML pipeline no longer spreads Feast/offline-store loading and schema validation across unrelated helper code.

![Temporal split service proof](../../pngs/repo_design_pattern_split_service.png)

**Figure: SplitService temporal split proof.** Capture `SplitService.split_by_time()`, `write_jsonl_splits()`, and `write_dataset_metadata()`. This proves the no-leakage temporal split and dataset-version metadata are a named service boundary.

### Pattern 5: Template Method / Lifecycle Service For BST Training

**Intent:** keep training and evaluation flow consistent while reusing shared batch movement, forward pass, and metric computation.

**External reference:** [Template method pattern](https://en.wikipedia.org/wiki/Template_method_pattern).

**Implementation:** Ray Tune still uses the single-node `Trainer` path through `run_training()`, while the final distributed proof uses `ModelLifecycleService` inside `train_loop_per_worker()`. Both paths keep the same high-level lifecycle shape: create dataset/loader, move batches to the device, call the BST forward pass, compute loss and ranking metrics, save the best checkpoint, and publish metrics. `ModelLifecycleService` adds the distributed concerns: `DistributedSampler`, DDP metric reduction, rank-0 checkpointing, metric broadcast, Ray Train reporting, and best-result writing.

| Code reference | What to point out in the screenshot |
|---|---|
| [apps/ml-system/src/models/trainer.py line 88](../../../apps/ml-system/src/models/trainer.py#L88) | `_move_batch_to_device()` shared step. |
| [apps/ml-system/src/models/trainer.py line 97](../../../apps/ml-system/src/models/trainer.py#L97) | `_forward_batch()` shared step. |
| [apps/ml-system/src/models/trainer.py line 129](../../../apps/ml-system/src/models/trainer.py#L129) | `train()` algorithm skeleton. |
| [apps/ml-system/src/models/trainer.py line 175](../../../apps/ml-system/src/models/trainer.py#L175) | `evaluate()` algorithm skeleton. |
| [apps/ml-system/src/models/trainer.py line 217](../../../apps/ml-system/src/models/trainer.py#L217) | `_compute_metrics()` shared metric step. |
| [apps/ml-system/src/training/ray_tune_train_bst.py line 207](../../../apps/ml-system/src/training/ray_tune_train_bst.py#L207) | `run_trial()` uses `run_training()` for Ray Tune trials and reports the best trial metrics. |
| [apps/ml-system/src/training/ray_distributed_train_bst.py line 104](../../../apps/ml-system/src/training/ray_distributed_train_bst.py#L104) | `ModelLifecycleService` groups DDP dataset, loader, train, eval, checkpoint, report, and best-result lifecycle methods. |
| [apps/ml-system/src/training/ray_distributed_train_bst.py line 315](../../../apps/ml-system/src/training/ray_distributed_train_bst.py#L315) | `train_loop_per_worker()` instantiates `ModelLifecycleService` for each Ray Train worker. |
| [apps/ml-system/src/training/ray_distributed_train_bst.py line 415](../../../apps/ml-system/src/training/ray_distributed_train_bst.py#L415) | `TorchTrainer` uses `train_loop_per_worker`, so the DDP run goes through the lifecycle service. |

![Template method training loop proof](../../pngs/repo_design_pattern_template_trainer.png)

**Figure: Ray Tune single-node training lifecycle proof.** Capture `Trainer.train()`, `Trainer.evaluate()`, and `ray_tune_train_bst.run_trial()`. This proves Ray Tune trials use a consistent training/evaluation lifecycle and report comparable metrics.

![DDP model lifecycle service proof](../../pngs/repo_design_pattern_model_lifecycle_service.png)

**Figure: DDP ModelLifecycleService proof.** Capture `ModelLifecycleService`, `train_loop_per_worker()`, and the `TorchTrainer(train_loop_per_worker=...)` call. This proves final distributed training uses the lifecycle service rather than ad hoc worker-loop code.

### Pattern 6: Composite Neural Network Module

**Intent:** build a complex BST recommender by composing smaller PyTorch modules.

**External reference:** [Composite pattern](https://en.wikipedia.org/wiki/Composite_pattern).

**Implementation:** `BST` combines embedding layers, `LightTransformerLayer`, positional encoding, MLP layers, and linear projections. Each piece remains a testable `nn.Module` or standard PyTorch layer.

| Code reference | What to point out in the screenshot |
|---|---|
| [apps/ml-system/src/models/model.py line 856](../../../apps/ml-system/src/models/model.py#L856) | `PositionalEncoding` is a reusable module. |
| [apps/ml-system/src/models/model.py line 886](../../../apps/ml-system/src/models/model.py#L886) | `BST` is the composite model. |
| [apps/ml-system/src/models/model.py line 893](../../../apps/ml-system/src/models/model.py#L893) | Entity embedding modules. |
| [apps/ml-system/src/models/model.py line 924](../../../apps/ml-system/src/models/model.py#L924) | Transformer layer composition. |
| [apps/ml-system/src/models/model.py line 938](../../../apps/ml-system/src/models/model.py#L938) | MLP composition with `nn.Sequential`. |

![Composite model design pattern proof](../../pngs/small_component_class.png)

![Composite model design pattern proof](../../pngs/low_level_bst_ranker_model.png)

**Figure: Composite neural module proof.** Capture the `BST.__init__()` block showing embeddings, transformer layer, positional encoding, and MLP. This proves the model is composed from smaller modules instead of one unstructured forward implementation.

### Pattern 7: Builder / Manifest Generator For Model Promotion

**Intent:** build a deployable model artifact in a repeatable order and emit a manifest that downstream CD can consume.

**External reference:** [Builder pattern](https://en.wikipedia.org/wiki/Builder_pattern).

**Implementation:** `promote_best_model()` orchestrates a deterministic sequence: read best Ray result, build Triton repository, choose versioned paths, build manifest, register MLflow model version, write/upload artifacts, and optionally promote `latest`.

| Code reference | What to point out in the screenshot |
|---|---|
| [apps/ml-system/src/registry/model_promotion.py line 405](../../../apps/ml-system/src/registry/model_promotion.py#L405) | `build_triton_repository()` assembles Triton model layout. |
| [apps/ml-system/src/registry/model_promotion.py line 471](../../../apps/ml-system/src/registry/model_promotion.py#L471) | `build_manifest()` constructs deployment metadata. |
| [apps/ml-system/src/registry/model_promotion.py line 511](../../../apps/ml-system/src/registry/model_promotion.py#L511) | `register_mlflow_model_version()` writes registry metadata. |
| [apps/ml-system/src/registry/model_promotion.py line 559](../../../apps/ml-system/src/registry/model_promotion.py#L559) | `promote_best_model()` coordinates the promotion flow. |

![Builder manifest design pattern proof](../../pngs/repo_design_pattern_builder_manifest.png)

**Figure: Builder/Manifest design pattern proof.** Capture `promote_best_model()`, `build_triton_repository()`, and `build_manifest()`. This proves deployment artifacts are assembled through a controlled builder flow, not by manual copy/paste steps.

### Pattern 8: Pipeline / Chain For Data Generation

**Intent:** make synthetic data generation a predictable sequence of independent processing stages.

**External reference:** [Pipeline / pipes-and-filters pattern](https://en.wikipedia.org/wiki/Pipeline_(software)).

**Implementation:** `HistoricalDataPipeline.run()` executes a clear chain: simulate data, inject challenges, validate invariants, write parquet tables, optionally write drift artifacts, then write a data-quality report and manifest.

| Code reference | What to point out in the screenshot |
|---|---|
| [apps/data-platform/data-generator/src/pipeline.py line 21](../../../apps/data-platform/data-generator/src/pipeline.py#L21) | `HistoricalDataPipeline` owns the generation flow. |
| [apps/data-platform/data-generator/src/pipeline.py line 26](../../../apps/data-platform/data-generator/src/pipeline.py#L26) | Simulation stage. |
| [apps/data-platform/data-generator/src/pipeline.py line 30](../../../apps/data-platform/data-generator/src/pipeline.py#L30) | Challenge injection stage. |
| [apps/data-platform/data-generator/src/pipeline.py line 40](../../../apps/data-platform/data-generator/src/pipeline.py#L40) | Validation stage. |
| [apps/data-platform/data-generator/src/pipeline.py line 51](../../../apps/data-platform/data-generator/src/pipeline.py#L51) | Sink/write stage. |
| [apps/data-platform/data-generator/src/pipeline.py line 116](../../../apps/data-platform/data-generator/src/pipeline.py#L116) | Manifest/report output stage. |

![Pipeline chain design pattern proof](../../pngs/repo_design_pattern_data_pipeline.png)

**Figure: Pipeline/Chain design pattern proof.** Capture `HistoricalDataPipeline.run()`. This proves the generator is structured as a sequence of explicit stages, which makes data-quality failures and drift artifact generation easier to reason about.


