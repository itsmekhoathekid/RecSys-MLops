# CI/CD

## CI/CD Strategy

The project uses path-based CI/CD. A single Jenkins Pipeline-from-SCM job receives
GitHub push or pull-request events, checks the changed paths, then runs only the
small CI/CD pipelines whose source paths were touched.

The root Jenkins pipeline is still one Jenkins job, but each rubric component is
handled as a separate branch under the shared stages:

```text
Checkout
  -> Detect Changed Components
  -> Python Env
  -> Component CI
  -> Component Build And Publish
  -> Component Deploy Or Update
```

`Component CI`, `Component Build And Publish`, and `Component Deploy Or Update`
fan out into Jenkins parallel branches such as `Materialize Pipeline`,
`Training Pipeline`, `DP1 Raw To Bronze`, `FastAPI Web API`, and
`KServe Inference Engine`. This is the evidence to capture: for every component,
open the Jenkins branch log under each of the three shared stages and screenshot
the successful Test, Build, and Deploy output.

Deploy is guarded so that changed components are deployed only from `main`, or
from a proof run with `FORCE_DEPLOY=true`. Images are tagged with the Git commit,
pushed to the configured registry, then injected into Helm/Kubeflow/KServe
deployments.

Jenkins UI is organized with seeded views from the CI Helm chart. `00 Main Auto
Deploy` contains `RecSys-GitHub-CICD`, the GitHub webhook job for push/merge
events. Views `01 Materialize Pipeline` to `09 Streaming Online Store` each
contain one manual proof job that uses the same `Jenkinsfile` with
`FORCE_COMPONENTS=<component>`, so the Test, Build, and Deploy evidence can be
captured per pipeline without mixing unrelated branches.

## Proof Capture Contract

For every CI/CD pipeline proof run, use `PUBLISH_IMAGES=true`,
`DEPLOY_CHANGED_COMPONENTS=true`, `FORCE_DEPLOY=true`, and
`REQUIRE_GCP_ARTIFACT_REGISTRY=true`. The GKE Jenkins values point both
`IMAGE_PUSH_REGISTRY` and `IMAGE_PULL_REGISTRY` at
`asia-southeast1-docker.pkg.dev/fsds-coursework/recsys`. The build script fails
fast if the registry is not a GCP Artifact Registry repository or if image push
is disabled.

Capture this proof set for every image-based component:

| Stage | Proof to capture | Expected evidence |
|---|---|---|
| Test | Image proof: Jenkins UI | `Component CI > <pipeline>` is green, pytest/contract/compile gate passed, coverage threshold satisfied where applicable. |
| Build | Image proof: Jenkins UI | `Component Build And Publish > <pipeline>` is green. |
| Build | Image build proof | Docker build logs, pushed image tag, and `.ci-image-manifest/<component>.env` showing the full `asia-southeast1-docker.pkg.dev/fsds-coursework/recsys/<image>:<git_commit>` URI. |
| Deploy | Image proof: Jenkins UI | `Component Deploy Or Update > <pipeline>` is green. |
| Deploy | Push into GCP Artifact Registry proof | GCP Artifact Registry UI or `gcloud artifacts docker images list` shows the pushed image tag. |
| Deploy | Update image proof | Jenkins deploy log shows the Helm/KFP/KServe value updated to the exact pushed image URI; data-platform pipelines also print the updated ConfigMap image key such as `DATAFLOW_IMAGE`, `SPARK_IMAGE`, or `FLINK_IMAGE`. |
| Deploy | Rolling update success proof | Jenkins deploy log shows `kubectl rollout status` success for long-running Deployments, or the equivalent KFP/RayJob/KServe readiness proof for non-Deployment artifacts. |

Recommended screenshot filenames:

```text
docs/pngs/cicd_<component>_test.png
docs/pngs/cicd_<component>_build.png
docs/pngs/cicd_<component>_image_build.png
docs/pngs/cicd_<component>_artifact_registry.png
docs/pngs/cicd_<component>_image_update.png
docs/pngs/cicd_<component>_rolling_update.png
docs/pngs/cicd_<component>_deploy.png
```

The only exception to "Docker image build" is the Triton/KServe branch:
`kserve` validates and deploys the promoted Triton model repository and
promotion manifest instead of building a custom serving image. Its equivalent
proof is the model promotion manifest, KServe `storageUri`, and
`InferenceService` readiness.

## Code Reference

- [Jenkinsfile line 1 (line 1)](../../../Jenkinsfile#L1): declares the CI/CD component list and the Jenkins UI branch labels.
- [Jenkinsfile line 17 (line 17)](../../../Jenkinsfile#L17): fans enabled components into parallel Jenkins branches.
- [Jenkinsfile line 35 (line 35)](../../../Jenkinsfile#L35): applies `FORCE_COMPONENTS` for one-pipeline proof jobs.
- [Jenkinsfile line 83 (line 83)](../../../Jenkinsfile#L83): gates deploy/update to `main` or `FORCE_DEPLOY=true`.
- [Jenkinsfile line 101 (line 101)](../../../Jenkinsfile#L101): declares Jenkins parameters, including `FORCE_COMPONENTS` for manual proof jobs.
- [Jenkinsfile line 132 (line 132)](../../../Jenkinsfile#L132): runs path detection, loads `.ci-components.env`, then optionally overrides with `FORCE_COMPONENTS`.
- [Jenkinsfile line 154 (line 154)](../../../Jenkinsfile#L154): runs the component Test stage.
- [Jenkinsfile line 166 (line 166)](../../../Jenkinsfile#L166): logs Docker in to GCP Artifact Registry with either Jenkins credentials or a GKE metadata access token.
- [Jenkinsfile line 179 (line 179)](../../../Jenkinsfile#L179): runs the component Build and Publish stage.
- [Jenkinsfile line 202 (line 202)](../../../Jenkinsfile#L202): runs the component Deploy or Update stage.
- [infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml line 124 (line 124)](../../../infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml#L124): enables the GitHub webhook trigger only on the main auto-deploy job.
- [infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml line 131 (line 131)](../../../infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml#L131): seeds the main webhook job, manual component proof jobs, and Jenkins views.
- [jenkins/scripts/detect_changed_components.py line 8 (line 8)](../../../jenkins/scripts/detect_changed_components.py#L8): defines all path-based CI/CD components.
- [jenkins/scripts/detect_changed_components.py line 96 (line 96)](../../../jenkins/scripts/detect_changed_components.py#L96): maps config changes to the affected component pipelines.
- [jenkins/scripts/detect_changed_components.py line 110 (line 110)](../../../jenkins/scripts/detect_changed_components.py#L110): maps data-platform source paths to DP/materialize/streaming components.
- [jenkins/scripts/detect_changed_components.py line 149 (line 149)](../../../jenkins/scripts/detect_changed_components.py#L149): maps infra/Helm/Kubeflow paths to deployable components.
- [jenkins/scripts/component_ci.sh line 1 (line 1)](../../../jenkins/scripts/component_ci.sh#L1): implements per-component test gates and coverage reports.
- [jenkins/scripts/component_build_publish.sh line 1 (line 1)](../../../jenkins/scripts/component_build_publish.sh#L1): builds, tags, pushes images to GCP Artifact Registry, and writes `.ci-image-manifest`.
- [jenkins/scripts/component_build_publish.sh line 16 (line 16)](../../../jenkins/scripts/component_build_publish.sh#L16): fails the build if the proof run is not pushing to GCP Artifact Registry.
- [jenkins/scripts/component_deploy.sh line 24 (line 24)](../../../jenkins/scripts/component_deploy.sh#L24): verifies updated workload images and waits for rollout status where a Deployment/StatefulSet exists.
- [jenkins/scripts/component_deploy.sh line 75 (line 75)](../../../jenkins/scripts/component_deploy.sh#L75): verifies data-platform ConfigMap image keys such as `DATAFLOW_IMAGE`, `SPARK_IMAGE`, and `FLINK_IMAGE`.
- [jenkins/scripts/component_deploy.sh line 107 (line 107)](../../../jenkins/scripts/component_deploy.sh#L107): updates Kubernetes, Helm, Kubeflow, Ray, and KServe runtime references with `--wait`.
- [jenkins/scripts/model_cd.py line 266 (line 266)](../../../jenkins/scripts/model_cd.py#L266): deploys the promoted Triton model from a promotion manifest to KServe.
- [infra/terraform/gcp/gke.tf line 8 (line 8)](../../../infra/terraform/gcp/gke.tf#L8): grants the GKE node service account Artifact Registry reader/writer permissions used by Jenkins image pull/push proof runs.

## CI/CD For Pipelines

### Materialize Pipeline

**Jenkins component:** `materialize`

**Jenkins UI label:** `Materialize Pipeline`

**Strategy:** run this CI/CD branch when materialization or Feast feature-store
paths change, especially `apps/data-platform/src/feature_store/`,
`apps/data-platform/src/local/`, feature-store repo/config files, data-platform
metadata code, or shared dataflow CLI Docker/runtime files.

**Test:** `component_ci.sh materialize` runs data-platform unit tests,
dataflow Docker contract tests, optional `tests/integration/materialize`, and
coverage for `feature_store.online_writer` and `local.run_batch_features`.

![Materialize Pipeline Test Jenkins UI proof](../../pngs/cicd_materialize_test.png)

**Figure: Materialize Pipeline Test proof.** Capture Jenkins
`Component CI > Materialize Pipeline` with passing pytest and coverage output.

**Build:** `component_build_publish.sh materialize` builds and pushes
`recsys-dataflow-cli:<git_commit>`.

![Materialize Pipeline Build Jenkins UI proof](../../pngs/cicd_materialize_build.png)

**Figure: Materialize Pipeline Build proof.** Capture Jenkins
`Component Build And Publish > Materialize Pipeline` with Docker build/push and
`.ci-image-manifest/materialize.env`.

**Deploy:** `component_deploy.sh materialize` runs Helm upgrade for
`recsys-data-platform` and updates `images.dataflowCli`.

![Materialize Pipeline Deploy Jenkins UI proof](../../pngs/cicd_materialize_deploy.png)

**Figure: Materialize Pipeline Deploy proof.** Capture Jenkins
`Component Deploy Or Update > Materialize Pipeline` with the Helm upgrade and
dataflow runtime image update.

### Training Pipeline

**Jenkins component:** `training`

**Jenkins UI label:** `Training Pipeline`

**Strategy:** run this CI/CD branch when ML training, Kubeflow pipeline, Ray
training, MLflow/runtime, BST config, or training image paths change, especially
`apps/ml-system/`, `infra/kubeflow/`, `infra/helm/ray-cluster/`,
`infra/helm/recsys-runtime/`, `infra/helm/mlflow-stack/`, and
`configs/local/bst.yaml`.

**Test:** `component_ci.sh training` runs ML-system tests, optional
`tests/integration/training`, coverage for Kubeflow pipeline helpers, and
compiles the KFP package.

![Training Pipeline Test Jenkins UI proof](../../pngs/cicd_training_test.png)

**Figure: Training Pipeline Test proof.** Capture Jenkins
`Component CI > Training Pipeline` showing `tests/unit/ml_system` and
`compile_training_pipeline.py` success.

**Build:** `component_build_publish.sh training` builds and pushes
`recsys-mlops-training:<git_commit>` and `recsys-mlops-spark:<git_commit>`.

![Training Pipeline Build Jenkins UI proof](../../pngs/cicd_training_build.png)

**Figure: Training Pipeline Build proof.** Capture Jenkins
`Component Build And Publish > Training Pipeline` with both training images
tagged by commit and pushed.

**Deploy:** `component_deploy.sh training` recompiles the KFP package with the
new image references and updates the Ray runtime chart `recsys-ray-cpu`.

![Training Pipeline Deploy Jenkins UI proof](../../pngs/cicd_training_deploy.png)

**Figure: Training Pipeline Deploy proof.** Capture Jenkins
`Component Deploy Or Update > Training Pipeline` showing updated
`RECSYS_PIPELINE_IMAGE`, `RECSYS_SPARK_IMAGE`, KFP compile, and Ray Helm update.

### Data Pipeline 1 - Source Data To Raw/Bronze

**Jenkins component:** `dp1`

**Jenkins UI label:** `DP1 Raw To Bronze`

**Strategy:** run this CI/CD branch when raw ingestion, synthetic data
generation, source Postgres/CDC, Kafka topic, Debezium, Kafka Connect, or raw
Airflow paths change, especially `apps/data-platform/data-generator/`,
`apps/data-platform/src/ingest/`, `raw_ingestion_dag.py`,
`configs/local/data_generator*.yaml`, `configs/local/postgres_source.yaml`, and
`configs/local/kafka_topics.yaml`.

**Test:** `component_ci.sh dp1` runs data-generator unit tests, ingest tests,
data-platform tests, Docker/dataflow contract tests, optional
`tests/integration/dp1`, and coverage for `ingest.debezium` and
`ingest.batch_lakehouse_ingestion`.

![DP1 Test Jenkins UI proof](../../pngs/cicd_dp1_test.png)

**Figure: DP1 Test proof.** Capture Jenkins `Component CI > DP1 Raw To Bronze`
showing generator/ingest tests and coverage.

**Build:** `component_build_publish.sh dp1` builds and pushes
`recsys-data-generator`, `recsys-dataflow-cli`, `recsys-airflow`, and
`recsys-kafka-connect`.

![DP1 Build Jenkins UI proof](../../pngs/cicd_dp1_build.png)

**Figure: DP1 Build proof.** Capture Jenkins
`Component Build And Publish > DP1 Raw To Bronze` with all DP1 images pushed.

**Deploy:** `component_deploy.sh dp1` upgrades `recsys-data-platform` and updates
the dataflow CLI, Airflow, and Kafka Connect images.

![DP1 Deploy Jenkins UI proof](../../pngs/cicd_dp1_deploy.png)

**Figure: DP1 Deploy proof.** Capture Jenkins
`Component Deploy Or Update > DP1 Raw To Bronze` showing the Helm upgrade for
source ingestion and CDC runtimes.

### Data Pipeline 2 - Bronze To Silver/Gold

**Jenkins component:** `dp2`

**Jenkins UI label:** `DP2 Bronze To Silver Gold`

**Strategy:** run this CI/CD branch when Spark silver/gold transforms, batch
feature DAGs, Spark batch config, lakehouse code, or Spark runtime image paths
change, especially `apps/data-platform/src/features/spark/`,
`batch_feature_pipeline_dag.py`, `apps/data-platform/src/lakehouse/`,
`apps/data-platform/Dockerfile.spark`, and `configs/local/spark_batch*.yaml`.

**Test:** `component_ci.sh dp2` runs data-platform tests, Docker/dataflow
contract tests, optional `tests/integration/dp2`, and coverage for
`lakehouse.iceberg`.

![DP2 Test Jenkins UI proof](../../pngs/cicd_dp2_test.png)

**Figure: DP2 Test proof.** Capture Jenkins
`Component CI > DP2 Bronze To Silver Gold` showing Spark/lakehouse tests and
coverage.

**Build:** `component_build_publish.sh dp2` builds and pushes `recsys-spark` and
`recsys-airflow`.

![DP2 Build Jenkins UI proof](../../pngs/cicd_dp2_build.png)

**Figure: DP2 Build proof.** Capture Jenkins
`Component Build And Publish > DP2 Bronze To Silver Gold` with Spark and Airflow
images pushed.

**Deploy:** `component_deploy.sh dp2` upgrades `recsys-data-platform` and updates
the Spark and Airflow images.

![DP2 Deploy Jenkins UI proof](../../pngs/cicd_dp2_deploy.png)

**Figure: DP2 Deploy proof.** Capture Jenkins
`Component Deploy Or Update > DP2 Bronze To Silver Gold` showing the Helm update
for batch transform runtimes.

### Data Pipeline 3 - Silver/Gold To Offline Feature Table

**Jenkins component:** `dp3`

**Jenkins UI label:** `DP3 Offline Feature Table`

**Strategy:** run this CI/CD branch when offline feature builders, training
table preparation, Feast/PostgreSQL offline-store export, DP3 Airflow DAG, or
offline feature table config changes, especially
`apps/data-platform/src/feature_store/`, `apps/data-platform/src/features/spark/`,
`apps/ml-system/src/cli/prepare_bst_training_data.py`,
`tests/unit/ml_system/test_prepare_bst_training_data.py`, and
`batch_feature_pipeline_dag.py`.

**Test:** `component_ci.sh dp3` runs data-platform tests, BST training-data prep
tests, Docker/dataflow contract tests, optional `tests/integration/dp3`, and
coverage for `lakehouse.iceberg` and `feature_store.online_writer`.

![DP3 Test Jenkins UI proof](../../pngs/cicd_dp3_test.png)

**Figure: DP3 Test proof.** Capture Jenkins
`Component CI > DP3 Offline Feature Table` showing offline feature table and BST
training-data tests.

**Build:** `component_build_publish.sh dp3` builds and pushes `recsys-spark`,
`recsys-dataflow-cli`, and `recsys-airflow`.

![DP3 Build Jenkins UI proof](../../pngs/cicd_dp3_build.png)

**Figure: DP3 Build proof.** Capture Jenkins
`Component Build And Publish > DP3 Offline Feature Table` with Spark, dataflow
CLI, and Airflow images pushed.

**Deploy:** `component_deploy.sh dp3` upgrades `recsys-data-platform` and updates
the Spark, dataflow CLI, and Airflow images.

![DP3 Deploy Jenkins UI proof](../../pngs/cicd_dp3_deploy.png)

**Figure: DP3 Deploy proof.** Capture Jenkins
`Component Deploy Or Update > DP3 Offline Feature Table` showing the offline
feature table runtime update.

## CI/CD For APIs

### Triton Inference Engine

**Jenkins component:** `kserve`

**Jenkins UI label:** `KServe Inference Engine`

**Strategy:** run this CI/CD branch when Triton/KServe serving chart, model
promotion, model CD script, or serving contract paths change, especially
`infra/helm/recsys-serving/`, `apps/ml-system/src/registry/model_promotion.py`,
`jenkins/scripts/model_cd.py`, `tests/unit/ml_system/test_model_promotion.py`,
and `tests/contract/test_serving_contracts.py`.

**CD pipeline trigger after retraining:** the Kubeflow retraining pipeline
exports a Triton model repository and writes a promotion manifest to
`s3://recsys-model-store/promotions/bst/production.json` or another
`PROMOTION_MANIFEST_URI`. The KServe CD branch consumes that manifest, validates
the required Triton files, renders serving Helm values, and rolls out the
KServe `InferenceService`.

**Test:** `component_ci.sh kserve` runs model promotion tests, serving contract
tests, optional `tests/integration/kserve`, and coverage for `model_cd`.

![Triton Inference Engine Test Jenkins UI proof](../../pngs/cicd_kserve_test.png)

**Figure: Triton Inference Engine Test proof.** Capture Jenkins
`Component CI > KServe Inference Engine` showing promotion and serving contract
tests.

**Build:** `component_build_publish.sh kserve` does not build an API image. The
artifact is the promoted Triton model repository plus promotion manifest.

![Triton Inference Engine Build Jenkins UI proof](../../pngs/cicd_kserve_build.png)

**Figure: Triton Inference Engine Build proof.** Capture Jenkins
`Component Build And Publish > KServe Inference Engine` showing that KServe uses
the Triton runtime and promoted model artifacts instead of a custom app image.

**Deploy:** `component_deploy.sh kserve` runs `model_cd.py`, validates the model
repository, applies `recsys-serving` Helm values, waits for
`recsys-bst-triton`, and enables KServe resource autoscaling after readiness.

![Triton Inference Engine Deploy Jenkins UI proof](../../pngs/cicd_kserve_deploy.png)

**Figure: Triton Inference Engine Deploy proof.** Capture Jenkins
`Component Deploy Or Update > KServe Inference Engine` showing the manifest URI,
KServe Helm upgrade, and `InferenceService` readiness.

### FastAPI For Online Features And Model Serving

**Jenkins component:** `api`

**Jenkins UI label:** `FastAPI Web API`

**Strategy:** run this CI/CD branch when API-serving source, ranking logic,
online feature client, A/B testing, API schemas, Triton client, serving chart, or
API tests change, especially `apps/api-serving/`,
`infra/helm/recsys-serving/`, `tests/unit/api_serving/`,
`tests/contract/test_serving_contracts.py`, and
`tests/contract/test_gateway_contracts.py`.

**Test:** `component_ci.sh api` runs API unit tests, serving contracts, gateway
contracts, optional `tests/integration/api`, and coverage for the FastAPI,
online feature API, ranking, A/B, and Triton client modules.

![FastAPI Test Jenkins UI proof](../../pngs/cicd_api_test.png)

**Figure: FastAPI Test proof.** Capture Jenkins `Component CI > FastAPI Web API`
showing API unit/contract tests and coverage above the threshold.

**Build:** `component_build_publish.sh api` builds and pushes
`recsys-api-serving:<git_commit>`.

![FastAPI Build Jenkins UI proof](../../pngs/cicd_api_build.png)

**Figure: FastAPI Build proof.** Capture Jenkins
`Component Build And Publish > FastAPI Web API` showing Docker build/push and
`.ci-image-manifest/api.env`.

**Deploy:** `component_deploy.sh api` upgrades `recsys-serving`, updates both
`api.image` and `featureApi.image`, then waits for
`recsys-api-serving` and `recsys-online-feature-api` rollouts.

![FastAPI Deploy Jenkins UI proof](../../pngs/cicd_api_deploy.png)

**Figure: FastAPI Deploy proof.** Capture Jenkins
`Component Deploy Or Update > FastAPI Web API` showing the Helm update and
rollout status for both FastAPI services.

## CI/CD For Jobs

### Job 1 - Push Stream Feature To OFFLINE Store

**Jenkins component:** `stream_offline`

**Jenkins UI label:** `Stream Features To Offline Store`

**Strategy:** run this CI/CD branch when Flink streaming jobs, Kafka realtime
processing code, offline feature sink logic, Flink Dockerfile, or streaming DAG
paths change, especially `apps/data-platform/src/features/flink/`,
`apps/data-platform/src/lakehouse/`, `apps/data-platform/Dockerfile.flink`,
`streaming_feature_pipeline_dag.py`, and `configs/local/flink_streaming.yaml`.

**Test:** `component_ci.sh stream_offline` runs data-platform tests,
Docker/dataflow contract tests, optional `tests/integration/stream_offline`, and
coverage for the Flink job modules plus the offline sink/lakehouse code.

![Stream Offline Test Jenkins UI proof](../../pngs/cicd_stream_offline_test.png)

**Figure: Stream Offline Test proof.** Capture Jenkins
`Component CI > Stream Features To Offline Store` showing Flink/offline sink
tests and coverage.

**Build:** `component_build_publish.sh stream_offline` builds and pushes
`recsys-flink:<git_commit>`.

![Stream Offline Build Jenkins UI proof](../../pngs/cicd_stream_offline_build.png)

**Figure: Stream Offline Build proof.** Capture Jenkins
`Component Build And Publish > Stream Features To Offline Store` showing Flink
image build/push.

**Deploy:** `component_deploy.sh stream_offline` upgrades
`recsys-data-platform` and updates `images.flink`, which rolls the continuous
Flink offline-store job.

![Stream Offline Deploy Jenkins UI proof](../../pngs/cicd_stream_offline_deploy.png)

**Figure: Stream Offline Deploy proof.** Capture Jenkins
`Component Deploy Or Update > Stream Features To Offline Store` showing the Helm
upgrade for the continuous Kafka-to-offline-store Flink job.

### Job 2 - Push Stream Feature To ONLINE Store

**Jenkins component:** `stream_online`

**Jenkins UI label:** `Stream Features To Online Store`

**Strategy:** run this CI/CD branch when Flink streaming jobs, Redis online
writer logic, online feature sink code, realtime API interaction, Flink
Dockerfile, or Redis online-store config changes, especially
`apps/data-platform/src/features/flink/`,
`apps/data-platform/src/feature_store/online_writer.py`,
`apps/data-platform/Dockerfile.dataflow-cli`,
`apps/data-platform/Dockerfile.flink`, `configs/local/flink_streaming.yaml`, and
`configs/local/redis_online_store.yaml`.

**Test:** `component_ci.sh stream_online` runs data-platform tests, selected API
serving tests, Docker/dataflow contract tests, optional
`tests/integration/stream_online`, and coverage for Flink job modules plus
`feature_store.online_writer`.

![Stream Online Test Jenkins UI proof](../../pngs/cicd_stream_online_test.png)

**Figure: Stream Online Test proof.** Capture Jenkins
`Component CI > Stream Features To Online Store` showing Flink/Redis online
writer tests and coverage.

**Build:** `component_build_publish.sh stream_online` builds and pushes
`recsys-flink:<git_commit>` and `recsys-dataflow-cli:<git_commit>`.

![Stream Online Build Jenkins UI proof](../../pngs/cicd_stream_online_build.png)

**Figure: Stream Online Build proof.** Capture Jenkins
`Component Build And Publish > Stream Features To Online Store` showing Flink
and dataflow CLI image build/push.

**Deploy:** `component_deploy.sh stream_online` upgrades
`recsys-data-platform`, updates `images.flink` and `images.dataflowCli`, and
rolls the continuous Flink online-store job and online writer runtime.

![Stream Online Deploy Jenkins UI proof](../../pngs/cicd_stream_online_deploy.png)

**Figure: Stream Online Deploy proof.** Capture Jenkins
`Component Deploy Or Update > Stream Features To Online Store` showing the Helm
upgrade for the continuous Kafka-to-Redis online-store Flink job.
