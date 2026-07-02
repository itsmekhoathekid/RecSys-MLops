# CI/CD

This document is written as a proof-capture checklist. For every CI/CD rubric
row, capture the same three stages:

1. **Test**: Jenkins `Component CI` branch proves unit, contract, compile, or
   integration gates pass.
2. **Build**: Jenkins `Component Build And Publish` branch proves the image or
   deployable artifact is built, tagged, pushed, and recorded.
3. **Deploy**: Jenkins `Component Deploy Or Update` branch proves the changed
   component is automatically rolled out to Kubernetes, Kubeflow, KServe, or the
   data platform runtime.

Secrets are not committed in source code. Jenkins receives registry and cluster
access through credentials such as `REGISTRY_CREDENTIALS_ID` and
`KUBECONFIG_CREDENTIALS_ID`.

## Common Jenkins Flow

Code references:

- [Jenkinsfile line 1](../../../Jenkinsfile#L1): defines CI/CD components and their labels.
- [Jenkinsfile line 53](../../../Jenkinsfile#L53): enables GitHub webhook trigger via `githubPush()`.
- [Jenkinsfile line 57](../../../Jenkinsfile#L57): declares build, publish, deploy, and credential parameters.
- [Jenkinsfile line 82](../../../Jenkinsfile#L82): detects changed components from changed paths.
- [Jenkinsfile line 106](../../../Jenkinsfile#L106): runs `Component CI`.
- [Jenkinsfile line 131](../../../Jenkinsfile#L131): runs `Component Build And Publish`.
- [Jenkinsfile line 144](../../../Jenkinsfile#L144): runs `Component Deploy Or Update`.
- [jenkins/scripts/component_ci.sh line 1](../../../jenkins/scripts/component_ci.sh#L1): component test gates.
- [jenkins/scripts/component_build_publish.sh line 1](../../../jenkins/scripts/component_build_publish.sh#L1): component build and image publish gates.
- [jenkins/scripts/component_deploy.sh line 1](../../../jenkins/scripts/component_deploy.sh#L1): component deploy gates.

Webhook flow:

```text
GitHub push/PR
  -> GitHub Webhook
  -> Jenkins /github-webhook/
  -> RecSys-GitHub-CICD job
  -> Jenkinsfile
  -> Detect Changed Components
  -> Component CI
  -> Component Build And Publish
  -> Component Deploy Or Update when branch is main
```

The `recsys-ci` Helm chart seeds the `RecSys-GitHub-CICD` Pipeline-from-SCM job
and installs the Jenkins GitHub, Git, Pipeline, and Stage View plugins needed for
this flow.

GKE webhook endpoint proof:

```text
Payload URL: http://34.21.171.234/github-webhook/
Ingress route: /github-webhook/ -> ci/recsys-jenkins:8080
Smoke result: HTTP/1.1 200 OK for a GitHub ping payload
```

Jenkins stages to capture:

```text
Checkout
Detect Changed Components
Python Env
Component CI
Component Build And Publish
Component Deploy Or Update
```

Use these Jenkins run parameters for proof runs when a webhook is not available:

```text
PUBLISH_IMAGES=true
DEPLOY_CHANGED_COMPONENTS=true
FORCE_DEPLOY=true
COVERAGE_MIN=90
IMAGE_PUSH_REGISTRY=<registry used by Jenkins>
IMAGE_PULL_REGISTRY=<registry used by Kubernetes>
```

For each rubric item below, capture three screenshots or log snippets:

```text
docs/pngs/cicd_<component>_test.png
docs/pngs/cicd_<component>_build.png
docs/pngs/cicd_<component>_deploy.png
```

Each screenshot should include the Jenkins branch label, the stage name, and the
last successful log lines.

## Jenkins UI Proof On GKE

The Jenkins controller is deployed in the `ci` namespace by `make
gcp-services-up` through the `recsys-ci` Helm chart. The same command also waits
for Jenkins, the in-cluster registry, and the registry node proxy before running
the rest of the service smoke checks.

Captured Jenkins UI proof:

| Proof | What it shows |
|---|---|
| ![Jenkins dashboard proof](../../pngs/cicd_jenkins_dashboard.png) | Jenkins UI is reachable on GKE and the `RecSys-CICD-Proof` pipeline exists. |
| ![Jenkins stage view proof](../../pngs/cicd_jenkins_stage_view.png) | Build `#2` is green and Stage View shows `Checkout`, `Detect Changed Components`, `Python Env`, `Component CI`, `Component Build And Publish`, and `Component Deploy Or Update` with the rubric component branches. |
| ![Jenkins build success proof](../../pngs/cicd_jenkins_build_success.png) | The successful build page shows the checked-out Git revision from the coursework repository. |
| ![Jenkins console proof](../../pngs/cicd_jenkins_console_success.png) | Console output shows the component deploy/update labels and ends with `Finished: SUCCESS`. |

## Proof Summary Table

| Rubric item | Jenkins component | Test proof | Build proof | Deploy proof |
|---|---|---|---|---|
| Materialize Pipeline | `materialize` | Feature-store/materialization tests pass | `recsys-dataflow-cli:<commit>` built and pushed | Helm updates `images.dataflowCli` |
| Training Pipeline | `training` | ML tests pass and KFP package compiles | `recsys-mlops-training:<commit>` and `recsys-mlops-spark:<commit>` built and pushed | KFP package and Ray runtime image refs updated |
| DP1 Raw To Bronze | `dp1` | Data-generator, CDC, ingestion tests pass | generator, dataflow CLI, Airflow, Kafka Connect images built and pushed | Helm updates DP1 data platform runtimes |
| DP2 Bronze To Silver/Gold | `dp2` | Spark batch feature tests pass | Spark and Airflow images built and pushed | Helm updates Spark batch/Airflow runtimes |
| DP3 Offline Feature Table | `dp3` | Offline feature/training-table tests pass | Spark, dataflow CLI, and Airflow images built and pushed | Helm updates DP3 runtimes |
| Web API with FastAPI | `api` | API unit and contract tests pass | `recsys-api-serving:<commit>` built and pushed | Helm deploy plus API rollout status |
| Inference Engine / KServe | `kserve` | model promotion and serving contract tests pass | promoted Triton model artifact is consumed | `model_cd.py` applies KServe/CD values and waits readiness |
| Real-time Drift Detection Web API | `drift` | drift reporting and retrain-trigger tests pass | `recsys-dataflow-cli:<commit>` built and pushed | Helm/Knative drift runtime updated |
| Job 1: Stream feature to OFFLINE store | `stream_offline` | Flink offline sink tests pass | `recsys-flink:<commit>` built and pushed | Helm updates realtime Flink consumer |
| Job 2: Stream feature to ONLINE store | `stream_online` | Flink online sink and Redis writer tests pass | Flink and dataflow CLI images built and pushed | Helm updates realtime Flink/online writer runtime |

## Per-Pipeline Capture Checklist

### 1. Materialize Pipeline

Jenkins branch label: `Materialize Pipeline`

Component command:

```bash
bash jenkins/scripts/component_ci.sh materialize
bash jenkins/scripts/component_build_publish.sh materialize
bash jenkins/scripts/component_deploy.sh materialize
```

#### Step 1 - Test Proof

Capture Jenkins stage:

```text
Component CI > Materialize Pipeline
```

Log keywords to include:

```text
tests/unit/data_platform/test_data_platform.py
tests/contract/test_docker_dataflow_contracts.py
coverage
--cov-fail-under=90
```

Proof image:

![Materialize test proof](../../pngs/cicd_materialize_test.png)

#### Step 2 - Build Proof

Capture Jenkins stage:

```text
Component Build And Publish > Materialize Pipeline
```

Log keywords to include:

```text
docker build
recsys-dataflow-cli:<commit>
docker push
.ci-image-manifest/materialize.env
```

Proof image:

![Materialize build proof](../../pngs/cicd_materialize_build.png)

#### Step 3 - Deploy Proof

Capture Jenkins stage:

```text
Component Deploy Or Update > Materialize Pipeline
```

Log keywords to include:

```text
helm upgrade --install recsys-data-platform
--set images.dataflowCli=<registry>/recsys-dataflow-cli:<commit>
```

Runtime verification:

```bash
kubectl get deploy -n recsys-dataflow airflow-webserver airflow-scheduler
```

Proof image:

![Materialize deploy proof](../../pngs/cicd_materialize_deploy.png)

### 2. Training Pipeline

Jenkins branch label: `Training Pipeline`

Component command:

```bash
bash jenkins/scripts/component_ci.sh training
bash jenkins/scripts/component_build_publish.sh training
bash jenkins/scripts/component_deploy.sh training
```

#### Step 1 - Test Proof

Capture Jenkins stage:

```text
Component CI > Training Pipeline
```

Log keywords to include:

```text
tests/unit/ml_system
compile_training_pipeline.py
infra/kubeflow/compiled/bst_training_pipeline.yaml
coverage
```

Proof image:

![Training test proof](../../pngs/cicd_training_test.png)

#### Step 2 - Build Proof

Capture Jenkins stage:

```text
Component Build And Publish > Training Pipeline
```

Log keywords to include:

```text
recsys-mlops-training:<commit>
recsys-mlops-spark:<commit>
docker push
.ci-image-manifest/training.env
```

Proof image:

![Training build proof](../../pngs/cicd_training_build.png)

#### Step 3 - Deploy Proof

Capture Jenkins stage:

```text
Component Deploy Or Update > Training Pipeline
```

Log keywords to include:

```text
RECSYS_PIPELINE_IMAGE=<registry>/recsys-mlops-training:<commit>
RECSYS_SPARK_IMAGE=<registry>/recsys-mlops-spark:<commit>
compile_training_pipeline.py
helm upgrade --install recsys-ray-cpu
```

Runtime verification:

```bash
kubectl get deploy -n kubeflow ml-pipeline ml-pipeline-ui workflow-controller kuberay-operator
kubectl get workflows -n kubeflow
```

Proof image:

![Training deploy proof](../../pngs/cicd_training_deploy.png)

### 3. DP1 - Raw Data Generator, CDC, And Historical Ingest

Jenkins branch label: `DP1 Raw To Bronze`

Component command:

```bash
bash jenkins/scripts/component_ci.sh dp1
bash jenkins/scripts/component_build_publish.sh dp1
bash jenkins/scripts/component_deploy.sh dp1
```

#### Step 1 - Test Proof

Capture Jenkins stage:

```text
Component CI > DP1 Raw To Bronze
```

Log keywords to include:

```text
tests/unit/data_generator
ingest.debezium
ingest.batch_lakehouse_ingestion
tests/contract/test_docker_dataflow_contracts.py
```

Proof image:

![DP1 test proof](../../pngs/cicd_dp1_test.png)

#### Step 2 - Build Proof

Capture Jenkins stage:

```text
Component Build And Publish > DP1 Raw To Bronze
```

Log keywords to include:

```text
recsys-data-generator:<commit>
recsys-dataflow-cli:<commit>
recsys-airflow:<commit>
recsys-kafka-connect:<commit>
docker push
.ci-image-manifest/dp1.env
```

Proof image:

![DP1 build proof](../../pngs/cicd_dp1_build.png)

#### Step 3 - Deploy Proof

Capture Jenkins stage:

```text
Component Deploy Or Update > DP1 Raw To Bronze
```

Log keywords to include:

```text
helm upgrade --install recsys-data-platform
--set images.dataflowCli=<registry>/recsys-dataflow-cli:<commit>
--set images.airflow=<registry>/recsys-airflow:<commit>
--set images.kafkaConnect=<registry>/recsys-kafka-connect:<commit>
```

Runtime verification:

```bash
kubectl get deploy -n recsys-dataflow kafka kafka-connect airflow-webserver airflow-scheduler
kubectl get statefulset -n recsys-dataflow data-source-postgres data-data-platform-minio
```

Proof image:

![DP1 deploy proof](../../pngs/cicd_dp1_deploy.png)

### 4. DP2 - Bronze To Silver/Gold Batch Features

Jenkins branch label: `DP2 Bronze To Silver Gold`

Component command:

```bash
bash jenkins/scripts/component_ci.sh dp2
bash jenkins/scripts/component_build_publish.sh dp2
bash jenkins/scripts/component_deploy.sh dp2
```

#### Step 1 - Test Proof

Capture Jenkins stage:

```text
Component CI > DP2 Bronze To Silver Gold
```

Log keywords to include:

```text
tests/unit/data_platform/test_data_platform.py
lakehouse.iceberg
Spark batch feature tests
coverage
```

Proof image:

![DP2 test proof](../../pngs/cicd_dp2_test.png)

#### Step 2 - Build Proof

Capture Jenkins stage:

```text
Component Build And Publish > DP2 Bronze To Silver Gold
```

Log keywords to include:

```text
recsys-spark:<commit>
recsys-airflow:<commit>
docker push
.ci-image-manifest/dp2.env
```

Proof image:

![DP2 build proof](../../pngs/cicd_dp2_build.png)

#### Step 3 - Deploy Proof

Capture Jenkins stage:

```text
Component Deploy Or Update > DP2 Bronze To Silver Gold
```

Log keywords to include:

```text
helm upgrade --install recsys-data-platform
--set images.spark=<registry>/recsys-spark:<commit>
--set images.airflow=<registry>/recsys-airflow:<commit>
```

Runtime verification:

```bash
kubectl get deploy -n recsys-dataflow airflow-webserver airflow-scheduler
kubectl exec -n recsys-dataflow deploy/airflow-webserver -- airflow dags list
```

Proof image:

![DP2 deploy proof](../../pngs/cicd_dp2_deploy.png)

### 5. DP3 - Offline Feature Table For Training

Jenkins branch label: `DP3 Offline Feature Table`

Component command:

```bash
bash jenkins/scripts/component_ci.sh dp3
bash jenkins/scripts/component_build_publish.sh dp3
bash jenkins/scripts/component_deploy.sh dp3
```

#### Step 1 - Test Proof

Capture Jenkins stage:

```text
Component CI > DP3 Offline Feature Table
```

Log keywords to include:

```text
tests/unit/ml_system/test_prepare_bst_training_data.py
feature_store.online_writer
lakehouse.iceberg
training table
coverage
```

Proof image:

![DP3 test proof](../../pngs/cicd_dp3_test.png)

#### Step 2 - Build Proof

Capture Jenkins stage:

```text
Component Build And Publish > DP3 Offline Feature Table
```

Log keywords to include:

```text
recsys-spark:<commit>
recsys-dataflow-cli:<commit>
recsys-airflow:<commit>
docker push
.ci-image-manifest/dp3.env
```

Proof image:

![DP3 build proof](../../pngs/cicd_dp3_build.png)

#### Step 3 - Deploy Proof

Capture Jenkins stage:

```text
Component Deploy Or Update > DP3 Offline Feature Table
```

Log keywords to include:

```text
helm upgrade --install recsys-data-platform
--set images.spark=<registry>/recsys-spark:<commit>
--set images.dataflowCli=<registry>/recsys-dataflow-cli:<commit>
--set images.airflow=<registry>/recsys-airflow:<commit>
```

Runtime verification:

```bash
kubectl get deploy -n recsys-dataflow airflow-webserver airflow-scheduler
kubectl exec -n recsys-dataflow deploy/airflow-webserver -- airflow dags list
```

Proof image:

![DP3 deploy proof](../../pngs/cicd_dp3_deploy.png)

### 6. Web API With FastAPI

Jenkins branch label: `FastAPI Web API`

Component command:

```bash
bash jenkins/scripts/component_ci.sh api
bash jenkins/scripts/component_build_publish.sh api
bash jenkins/scripts/component_deploy.sh api
```

#### Step 1 - Test Proof

Capture Jenkins stage:

```text
Component CI > FastAPI Web API
```

Log keywords to include:

```text
tests/unit/api_serving
tests/contract/test_serving_contracts.py
tests/contract/test_gateway_contracts.py
ab_testing
ranking
triton
coverage
```

Proof image:

![API test proof](../../pngs/cicd_api_test.png)

#### Step 2 - Build Proof

Capture Jenkins stage:

```text
Component Build And Publish > FastAPI Web API
```

Log keywords to include:

```text
recsys-api-serving:<commit>
docker build
docker push
.ci-image-manifest/api.env
```

Proof image:

![API build proof](../../pngs/cicd_api_build.png)

#### Step 3 - Deploy Proof

Capture Jenkins stage:

```text
Component Deploy Or Update > FastAPI Web API
```

Log keywords to include:

```text
helm upgrade --install recsys-serving
--set api.image=<registry>/recsys-api-serving:<commit>
kubectl rollout status deployment/recsys-api-serving -n api-serving
```

Runtime verification:

```bash
kubectl get deploy -n api-serving recsys-api-serving
kubectl -n api-serving exec deploy/recsys-api-serving -c api -- \
  python -c 'import requests; r=requests.get("http://127.0.0.1:8080/healthz", timeout=10); print(r.status_code, r.text); r.raise_for_status()'
```

Proof image:

![API deploy proof](../../pngs/cicd_api_deploy.png)

### 7. Inference Engine / KServe

Jenkins branch label: `KServe Inference Engine`

Component command:

```bash
bash jenkins/scripts/component_ci.sh kserve
bash jenkins/scripts/component_build_publish.sh kserve
bash jenkins/scripts/component_deploy.sh kserve
```

#### Step 1 - Test Proof

Capture Jenkins stage:

```text
Component CI > KServe Inference Engine
```

Log keywords to include:

```text
tests/unit/ml_system/test_model_promotion.py
tests/contract/test_serving_contracts.py
model_cd
model promotion
coverage
```

Proof image:

![KServe test proof](../../pngs/cicd_kserve_test.png)

#### Step 2 - Build / Artifact Proof

Capture Jenkins stage:

```text
Component Build And Publish > KServe Inference Engine
```

Log keywords to include:

```text
KServe uses Triton runtime plus model artifacts
no application image build is required
.ci-image-manifest/kserve.env
PROMOTION_MANIFEST_URI
```

This build step proves that KServe consumes a promoted Triton model artifact
instead of rebuilding an API image.

Proof image:

![KServe artifact proof](../../pngs/cicd_kserve_build.png)

#### Step 3 - Deploy Proof

Capture Jenkins stage:

```text
Component Deploy Or Update > KServe Inference Engine
```

Log keywords to include:

```text
uv run python jenkins/scripts/model_cd.py
--manifest-uri s3://recsys-model-store/promotions/bst/production.json
--apply
InferenceService
recsys-bst-triton
Ready
```

Runtime verification:

```bash
kubectl get inferenceservice -n kserve-triton-inference
kubectl get pods -n kserve-triton-inference -L serving.kserve.io/inferenceservice,recsys.ai/ab-variant
```

Proof image:

![KServe deploy proof](../../pngs/cicd_kserve_deploy.png)

### 8. Real-Time Drift Detection Web API

Jenkins branch label: `Realtime Drift Detection`

Component command:

```bash
bash jenkins/scripts/component_ci.sh drift
bash jenkins/scripts/component_build_publish.sh drift
bash jenkins/scripts/component_deploy.sh drift
```

#### Step 1 - Test Proof

Capture Jenkins stage:

```text
Component CI > Realtime Drift Detection
```

Log keywords to include:

```text
tests/unit/data_generator/test_drift_reporting_unit.py
tests/unit/data_platform/test_data_platform.py
drift.reporting
drift.controller
retrain trigger
coverage
```

Proof image:

![Drift test proof](../../pngs/cicd_drift_test.png)

#### Step 2 - Build Proof

Capture Jenkins stage:

```text
Component Build And Publish > Realtime Drift Detection
```

Log keywords to include:

```text
recsys-dataflow-cli:<commit>
docker build
docker push
.ci-image-manifest/drift.env
```

Proof image:

![Drift build proof](../../pngs/cicd_drift_build.png)

#### Step 3 - Deploy Proof

Capture Jenkins stage:

```text
Component Deploy Or Update > Realtime Drift Detection
```

Log keywords to include:

```text
helm upgrade --install recsys-data-platform
--set images.dataflowCli=<registry>/recsys-dataflow-cli:<commit>
kubectl apply -k infra/knative/recsys-drift
No infra/knative/recsys-drift manifests yet; deployed drift-capable dataflow image only.
```

Runtime verification:

```bash
kubectl get deploy -n recsys-dataflow airflow-webserver airflow-scheduler
kubectl get deploy -n observability recsys-pushgateway recsys-prometheus recsys-grafana
```

Proof image:

![Drift deploy proof](../../pngs/cicd_drift_deploy.png)

### 9. Job 1 - Push Stream Feature To OFFLINE Store

Jenkins branch label: `Stream Features To Offline Store`

Component command:

```bash
bash jenkins/scripts/component_ci.sh stream_offline
bash jenkins/scripts/component_build_publish.sh stream_offline
bash jenkins/scripts/component_deploy.sh stream_offline
```

#### Step 1 - Test Proof

Capture Jenkins stage:

```text
Component CI > Stream Features To Offline Store
```

Log keywords to include:

```text
features.flink.candidate_pool_job
features.flink.item_features_job
features.flink.user_aggregate_job
features.flink.user_sequence_job
lakehouse.iceberg
coverage
```

Proof image:

![Stream offline test proof](../../pngs/cicd_stream_offline_test.png)

#### Step 2 - Build Proof

Capture Jenkins stage:

```text
Component Build And Publish > Stream Features To Offline Store
```

Log keywords to include:

```text
recsys-flink:<commit>
docker build
docker push
.ci-image-manifest/stream_offline.env
```

Proof image:

![Stream offline build proof](../../pngs/cicd_stream_offline_build.png)

#### Step 3 - Deploy Proof

Capture Jenkins stage:

```text
Component Deploy Or Update > Stream Features To Offline Store
```

Log keywords to include:

```text
helm upgrade --install recsys-data-platform
--set images.flink=<registry>/recsys-flink:<commit>
```

Runtime verification:

```bash
kubectl get deploy -n recsys-dataflow realtime-flink-consumer flink-jobmanager flink-taskmanager
kubectl exec -n recsys-dataflow deploy/flink-jobmanager -- curl -s http://localhost:8081/jobs/overview
```

Proof image:

![Stream offline deploy proof](../../pngs/cicd_stream_offline_deploy.png)

### 10. Job 2 - Push Stream Feature To ONLINE Store

Jenkins branch label: `Stream Features To Online Store`

Component command:

```bash
bash jenkins/scripts/component_ci.sh stream_online
bash jenkins/scripts/component_build_publish.sh stream_online
bash jenkins/scripts/component_deploy.sh stream_online
```

#### Step 1 - Test Proof

Capture Jenkins stage:

```text
Component CI > Stream Features To Online Store
```

Log keywords to include:

```text
features.flink.candidate_pool_job
features.flink.item_features_job
features.flink.user_aggregate_job
features.flink.user_sequence_job
feature_store.online_writer
tests/unit/api_serving/test_serving.py
coverage
```

Proof image:

![Stream online test proof](../../pngs/cicd_stream_online_test.png)

#### Step 2 - Build Proof

Capture Jenkins stage:

```text
Component Build And Publish > Stream Features To Online Store
```

Log keywords to include:

```text
recsys-flink:<commit>
recsys-dataflow-cli:<commit>
docker push
.ci-image-manifest/stream_online.env
```

Proof image:

![Stream online build proof](../../pngs/cicd_stream_online_build.png)

#### Step 3 - Deploy Proof

Capture Jenkins stage:

```text
Component Deploy Or Update > Stream Features To Online Store
```

Log keywords to include:

```text
helm upgrade --install recsys-data-platform
--set images.flink=<registry>/recsys-flink:<commit>
--set images.dataflowCli=<registry>/recsys-dataflow-cli:<commit>
```

Runtime verification:

```bash
kubectl get deploy -n recsys-dataflow realtime-flink-consumer redis
kubectl exec -n recsys-dataflow deploy/redis -- redis-cli DBSIZE
```

Proof image:

![Stream online deploy proof](../../pngs/cicd_stream_online_deploy.png)

## Optional Supporting Component - Spark Batch Processing

This component is not always listed as a separate final-coursework row, but it
supports DP2 and DP3.

Jenkins branch label: `Spark Batch Processing`

Component command:

```bash
bash jenkins/scripts/component_ci.sh spark_batch
bash jenkins/scripts/component_build_publish.sh spark_batch
bash jenkins/scripts/component_deploy.sh spark_batch
```

Capture:

| Step | Jenkins stage | Proof image |
|---|---|---|
| Test | `Component CI > Spark Batch Processing` | `../../pngs/cicd_spark_batch_test.png` |
| Build | `Component Build And Publish > Spark Batch Processing` | `../../pngs/cicd_spark_batch_build.png` |
| Deploy | `Component Deploy Or Update > Spark Batch Processing` | `../../pngs/cicd_spark_batch_deploy.png` |

## Final Runtime Verification After Deploy

After capturing Jenkins logs, capture one cluster runtime screenshot proving the
deployed components are alive:

```bash
kubectl get deploy -n recsys-dataflow
kubectl get deploy -n api-serving
kubectl get inferenceservice -n kserve-triton-inference
kubectl get pods -n kserve-triton-inference -L serving.kserve.io/inferenceservice,recsys.ai/ab-variant
kubectl get deploy -n observability
kubectl get pods -n kubeflow
```

Expected GKE result when services are up:

```text
recsys-dataflow:
airflow-scheduler, airflow-webserver, flink-jobmanager, flink-taskmanager,
kafka, kafka-connect, realtime-event-producer, realtime-flink-consumer,
redis, and zookeeper are READY.

api-serving:
recsys-api-serving is READY.

kserve-triton-inference:
recsys-bst-triton and recsys-bst-triton-candidate are READY.

observability:
recsys-prometheus, recsys-grafana, recsys-pushgateway, recsys-loki,
recsys-tempo, and exporters are READY.

kubeflow:
ml-pipeline, ml-pipeline-ui, workflow-controller, and kuberay-operator are
Running.
```

Final runtime proof images:

![CI/CD runtime dataflow proof](../../pngs/cicd_runtime_dataflow.png)

![CI/CD runtime serving proof](../../pngs/cicd_runtime_serving.png)

![CI/CD runtime ml and observability proof](../../pngs/cicd_runtime_ml_observability.png)

## Capture Rule For Reviewer

For each rubric row, submit three proof screenshots:

```text
<component>_test.png   -> Jenkins Component CI log is green and shows tests.
<component>_build.png  -> Jenkins Build And Publish log is green and shows image/artifact.
<component>_deploy.png -> Jenkins Deploy Or Update log is green and shows rollout/update.
```

The deploy screenshot should be paired with a Kubernetes/Kubeflow/KServe runtime
check when the deployment target is user-facing or long-running.
