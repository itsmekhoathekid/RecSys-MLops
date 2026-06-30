# CI/CD

CI/CD in this project means: run tests, build/version artifacts, and auto-deploy the changed component. Secrets are not stored in source code. Jenkins receives registry and Kubernetes access through credentials such as `REGISTRY_CREDENTIALS_ID` and `KUBECONFIG_CREDENTIALS_ID`. For the GCP proof, Docker image builds run on **Google Cloud Build** instead of local Docker.

Common Jenkins flow:

1. `Detect Changed Components`: maps changed files to CI/CD components.
2. `Component CI`: runs unit/contract tests and coverage checks.
3. `Build And Publish`: builds Docker images on Cloud Build, pushes them to Artifact Registry, or records that the component consumes promoted model artifacts.
4. `Deploy Or Update`: updates Helm releases, KFP packages, KServe services, or Flink jobs.

Common code reference:

- [Jenkinsfile line 3](../../../Jenkinsfile#L3): declares all CI/CD components.
- [Jenkinsfile line 53](../../../Jenkinsfile#L53): defines build/deploy parameters and Jenkins credential IDs.
- [Jenkinsfile line 102](../../../Jenkinsfile#L102): runs `Component CI`.
- [Jenkinsfile line 127](../../../Jenkinsfile#L127): runs `Build And Publish`.
- [Jenkinsfile line 140](../../../Jenkinsfile#L140): runs `Deploy Or Update`.
- [jenkins/scripts/detect_changed_components.py line 244](../../../jenkins/scripts/detect_changed_components.py#L244): writes the selected component flags for Jenkins.
- [jenkins/README.md line 46](../../../jenkins/README.md#L46): documents that secrets are provided through Jenkins credentials.
- [infra/helm/recsys-ci/templates/jenkins.yaml line 137](../../../infra/helm/recsys-ci/templates/jenkins.yaml#L137): exposes the Jenkins service.
- [infra/helm/recsys-ci/templates/jenkins-secret.yaml line 10](../../../infra/helm/recsys-ci/templates/jenkins-secret.yaml#L10): stores Jenkins admin credentials in a Kubernetes Secret, not in source code.
- [infra/cloudbuild/recsys-images.yaml](../../../infra/cloudbuild/recsys-images.yaml): builds and pushes the GCP images through Cloud Build.

## GCP Cloud Build Proof Run

This is the image-build proof for all components that need container images. It does not require Docker on the local machine.

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

gcloud builds submit \
  --project fsds-coursework \
  --config infra/cloudbuild/recsys-images.yaml \
  --substitutions _IMAGE_REPO=asia-southeast1-docker.pkg.dev/fsds-coursework/recsys,_TAG=gcp \
  --async \
  .
```

Current proof run:

```text
Build ID: 5793bfd4-3733-4506-a2f1-88535ca012d0
Project:  fsds-coursework
Status:   SUCCESS
Finished: 2026-06-30T13:01:28Z
Logs:     https://console.cloud.google.com/cloud-build/builds/5793bfd4-3733-4506-a2f1-88535ca012d0?project=455131526306
Final log: DONE
Digest:   sha256:569f3eb3e0bfcaa2d1068d1653e8edfeecf7aca1943fe6635c7d3b5262e082ec
```

Commands for checking logs/status:

```bash
gcloud builds describe 5793bfd4-3733-4506-a2f1-88535ca012d0 \
  --project fsds-coursework \
  --format='value(status,logUrl)'

# Optional if the gcloud beta component is installed.
gcloud beta builds log 5793bfd4-3733-4506-a2f1-88535ca012d0 --project fsds-coursework --stream

# Works without installing beta commands; useful for checking the latest step logs.
gcloud logging read 'resource.type="build" AND resource.labels.build_id="5793bfd4-3733-4506-a2f1-88535ca012d0"' \
  --project fsds-coursework \
  --limit=50 \
  --format='value(timestamp,textPayload)'
```

If `gcloud beta` is not installed, open the Cloud Console log URL above and expand each step by id.

Cloud Build step mapping:

| Cloud Build step | Images produced | Rubric components covered |
|---|---|---|
| `base-python` | `recsys-base-python:gcp` | Base image for dataflow/training. |
| `dataflow-cli` | `recsys-dataflow-cli:gcp` | Materialize pipeline, DP jobs, drift, online writer support. |
| `training` | `recsys-mlops-training:gcp` | Training pipeline. |
| `api-serving` | `recsys-api-serving:gcp` | Web API with FastAPI. |
| `kafka-connect` | `recsys-kafka-connect:gcp` | DP1 CDC/ingestion. |
| `mlflow` | `recsys-mlflow:gcp` | Experiment tracking runtime used by training/model registry. |
| `airflow` | `recsys-airflow:gcp` | Materialize, DP1, DP2, DP3 orchestration. |
| `spark` | `recsys-spark:gcp` | DP2 batch feature pipeline and DP3 training table preparation. |
| `flink` | `recsys-flink:gcp` | Stream feature to offline and online stores. |

Artifact Registry verification:

```bash
gcloud artifacts docker images list \
  asia-southeast1-docker.pkg.dev/fsds-coursework/recsys \
  --include-tags \
  --filter='tags:gcp' \
  --format='table(package,tags,updateTime)'
```

Observed result:

```text
IMAGE                                                                        TAGS  UPDATE_TIME
asia-southeast1-docker.pkg.dev/fsds-coursework/recsys/recsys-airflow         gcp   2026-06-30T19:59:48
asia-southeast1-docker.pkg.dev/fsds-coursework/recsys/recsys-api-serving     gcp   2026-06-30T19:57:56
asia-southeast1-docker.pkg.dev/fsds-coursework/recsys/recsys-base-python     gcp   2026-06-30T19:54:31
asia-southeast1-docker.pkg.dev/fsds-coursework/recsys/recsys-dataflow-cli    gcp   2026-06-30T19:55:58
asia-southeast1-docker.pkg.dev/fsds-coursework/recsys/recsys-flink           gcp   2026-06-30T20:01:27
asia-southeast1-docker.pkg.dev/fsds-coursework/recsys/recsys-kafka-connect   gcp   2026-06-30T19:58:16
asia-southeast1-docker.pkg.dev/fsds-coursework/recsys/recsys-mlflow          gcp   2026-06-30T19:59:28
asia-southeast1-docker.pkg.dev/fsds-coursework/recsys/recsys-mlops-training  gcp   2026-06-30T19:57:28
asia-southeast1-docker.pkg.dev/fsds-coursework/recsys/recsys-spark           gcp   2026-06-30T20:00:28
```

Image proof to capture:

```text
docs/pngs/cicd_cloud_build_steps.png
docs/pngs/cicd_artifact_registry_gcp_tags.png
```

## Rubric Log Checklist

Use this table while opening the Jenkins/Cloud Build logs. Each row corresponds to one rubric line in the screenshot.

| Rubric row | Component | CI test log | Build log | Deploy/runtime log |
|---|---|---|---|---|
| Materialize Pipeline | `materialize` | `COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh materialize` | Cloud Build `dataflow-cli` | `IMAGE_PULL_REGISTRY=asia-southeast1-docker.pkg.dev/fsds-coursework/recsys IMAGE_TAG=gcp bash jenkins/scripts/component_deploy.sh materialize` |
| Training Pipeline | `training` | `COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh training` | Cloud Build `training` | `IMAGE_PULL_REGISTRY=asia-southeast1-docker.pkg.dev/fsds-coursework/recsys IMAGE_TAG=gcp bash jenkins/scripts/component_deploy.sh training` |
| DP1 | `dp1` | `COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh dp1` | Cloud Build `dataflow-cli`, `kafka-connect`, `airflow` | `IMAGE_PULL_REGISTRY=asia-southeast1-docker.pkg.dev/fsds-coursework/recsys IMAGE_TAG=gcp bash jenkins/scripts/component_deploy.sh dp1` |
| DP2 | `dp2` | `COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh dp2` | Cloud Build `spark`, `airflow` | `IMAGE_PULL_REGISTRY=asia-southeast1-docker.pkg.dev/fsds-coursework/recsys IMAGE_TAG=gcp bash jenkins/scripts/component_deploy.sh dp2` |
| DP3 | `dp3` | `COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh dp3` | Cloud Build `spark`, `dataflow-cli`, `airflow` | `IMAGE_PULL_REGISTRY=asia-southeast1-docker.pkg.dev/fsds-coursework/recsys IMAGE_TAG=gcp bash jenkins/scripts/component_deploy.sh dp3` |
| Web API with FastAPI | `api` | `COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh api` | Cloud Build `api-serving` | `IMAGE_PULL_REGISTRY=asia-southeast1-docker.pkg.dev/fsds-coursework/recsys IMAGE_TAG=gcp bash jenkins/scripts/component_deploy.sh api` and `bash jenkins/scripts/post_deploy_e2e.sh` |
| Inference Engine / KServe | `kserve` | `COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh kserve` | No app image; consumes promoted Triton artifact | `PROMOTION_MANIFEST_URI=s3://recsys-model-store/promotions/bst/production.json bash jenkins/scripts/component_deploy.sh kserve` |
| Real-time Drift Detection Web API | `drift` | `COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh drift` | Cloud Build `dataflow-cli` | `IMAGE_PULL_REGISTRY=asia-southeast1-docker.pkg.dev/fsds-coursework/recsys IMAGE_TAG=gcp bash jenkins/scripts/component_deploy.sh drift` |
| Job 1: Push stream feature to OFFLINE store | `stream_offline` | `COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh stream_offline` | Cloud Build `flink` | `IMAGE_PULL_REGISTRY=asia-southeast1-docker.pkg.dev/fsds-coursework/recsys IMAGE_TAG=gcp bash jenkins/scripts/component_deploy.sh stream_offline` |
| Job 2: Push stream feature to ONLINE store | `stream_online` | `COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh stream_online` | Cloud Build `flink`, `dataflow-cli` | `IMAGE_PULL_REGISTRY=asia-southeast1-docker.pkg.dev/fsds-coursework/recsys IMAGE_TAG=gcp bash jenkins/scripts/component_deploy.sh stream_online` |

Cluster verification after deploy:

```bash
kubectl get deploy -n recsys-dataflow
kubectl get deploy -n api-serving
kubectl get inferenceservice -n kserve-triton-inference
kubectl get pods -n recsys-dataflow -l app=realtime-flink-consumer
kubectl get pods -n kubeflow
```

Observed GKE runtime result:

```text
recsys-dataflow deployments:
airflow-scheduler, airflow-webserver, flink-jobmanager, flink-taskmanager,
kafka, kafka-connect, realtime-event-producer, realtime-flink-consumer,
redis, and zookeeper are READY 1/1.

api-serving deployments:
recsys-api-serving is READY 1/1.

kserve-triton-inference InferenceServices:
recsys-bst-triton is READY True.
recsys-bst-triton-candidate is READY True.

stream jobs:
realtime-flink-consumer is 1/1 Running.

kubeflow:
ml-pipeline, ml-pipeline-ui, workflow-controller, and kuberay-operator are Running.
```

Runtime image proof to capture:

```text
docs/pngs/cicd_gke_runtime_dataflow.png
docs/pngs/cicd_gke_runtime_api_kserve.png
docs/pngs/cicd_gke_runtime_kubeflow_stream.png
```

Run through Jenkins UI:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

kubectl get pods -n ci -l app.kubernetes.io/name=recsys-jenkins
kubectl port-forward -n ci svc/recsys-jenkins 8086:8080

kubectl get secret -n ci recsys-jenkins-admin \
  -o jsonpath='{.data.password}' | base64 -d
```

Current GCP note: the `ci` namespace is not running a Jenkins pod in the current cluster proof, so the screenshot proof should use Cloud Build logs plus GKE deploy/runtime verification. The Jenkinsfile and scripts remain the CI orchestrator implementation, and the per-component commands below are the same scripts Jenkins runs inside `Component CI` and `Deploy Or Update`.

## CI/CD For Pipelines

### Materialize Pipeline

Code reference:

- [Jenkinsfile line 3](../../../Jenkinsfile#L3): defines the `materialize` CI/CD component.
- [jenkins/scripts/component_ci.sh line 94](../../../jenkins/scripts/component_ci.sh#L94): runs tests for feature-store materialization.
- [jenkins/scripts/component_build_publish.sh line 99](../../../jenkins/scripts/component_build_publish.sh#L99): builds the dataflow CLI image used by materialization.
- [jenkins/scripts/component_deploy.sh line 92](../../../jenkins/scripts/component_deploy.sh#L92): deploys the materialization runtime image to the data platform chart.
- [apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py line 205](../../../apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py#L205): Airflow task runs Feast `materialize-incremental`.

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh materialize
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" PUBLISH_IMAGES=1 bash jenkins/scripts/component_build_publish.sh materialize
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" bash jenkins/scripts/component_deploy.sh materialize
```

Description of output when running command:

- The CI step should show feature-store tests passing.
- The build step should produce a commit-tagged dataflow CLI image.
- The deploy step should update the data platform chart so Airflow uses the image that contains the Feast incremental materialization code.

Image proof:

![Materialize pipeline CI/CD success](../../pngs/cicd_materialize_pipeline.png)

### Training Pipeline

Code reference:

- [Jenkinsfile line 4](../../../Jenkinsfile#L4): defines the `training` CI/CD component.
- [jenkins/scripts/component_ci.sh line 101](../../../jenkins/scripts/component_ci.sh#L101): runs ML-system tests and compiles KFP.
- [jenkins/scripts/component_build_publish.sh line 103](../../../jenkins/scripts/component_build_publish.sh#L103): builds the training and Spark images.
- [jenkins/scripts/component_deploy.sh line 55](../../../jenkins/scripts/component_deploy.sh#L55): compiles/deploys training references and Ray runtime.
- [apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py line 213](../../../apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py#L213): defines the KFP training pipeline.

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh training
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" PUBLISH_IMAGES=1 bash jenkins/scripts/component_build_publish.sh training
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" bash jenkins/scripts/component_deploy.sh training
```

Description of output when running command:

- The CI step validates ML code and compiles `infra/kubeflow/compiled/bst_training_pipeline.yaml`.
- The build step creates commit-tagged training images.
- The deploy step updates Ray/KFP runtime references so the Kubeflow training pipeline uses the new image version.

Image proof:

![Training pipeline CI/CD success](../../pngs/cicd_training_pipeline.png)

### DP1 - Raw Data Generator, CDC, And Historical Ingest

Code reference:

- [Jenkinsfile line 6](../../../Jenkinsfile#L6): defines the `dp1` CI/CD component.
- [jenkins/scripts/component_ci.sh line 114](../../../jenkins/scripts/component_ci.sh#L114): runs data-generator and ingestion tests.
- [jenkins/scripts/component_build_publish.sh line 111](../../../jenkins/scripts/component_build_publish.sh#L111): builds generator, dataflow CLI, Airflow, and Kafka Connect images.
- [jenkins/scripts/component_deploy.sh line 98](../../../jenkins/scripts/component_deploy.sh#L98): deploys data platform images needed by DP1.

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh dp1
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" PUBLISH_IMAGES=1 bash jenkins/scripts/component_build_publish.sh dp1
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" bash jenkins/scripts/component_deploy.sh dp1
```

Description of output when running command:

- The CI step validates raw data generation, source schema, CDC connector, and ingestion logic.
- The build step publishes the images used by generator, Airflow orchestration, Kafka Connect, and dataflow CLI.
- The deploy step updates the data platform release so DP1 can run from Jenkins/Airflow with the latest code.

Image proof:

![DP1 CI/CD success](../../pngs/cicd_dp1.png)

### DP2 - Spark Batch Feature Materialization

Code reference:

- [Jenkinsfile line 7](../../../Jenkinsfile#L7): defines the `dp2` CI/CD component.
- [jenkins/scripts/component_ci.sh line 121](../../../jenkins/scripts/component_ci.sh#L121): runs Spark batch feature tests.
- [jenkins/scripts/component_build_publish.sh line 117](../../../jenkins/scripts/component_build_publish.sh#L117): builds Spark and Airflow images.
- [jenkins/scripts/component_deploy.sh line 95](../../../jenkins/scripts/component_deploy.sh#L95): deploys Spark batch and Airflow image updates.

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh dp2
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" PUBLISH_IMAGES=1 bash jenkins/scripts/component_build_publish.sh dp2
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" bash jenkins/scripts/component_deploy.sh dp2
```

Description of output when running command:

- The CI step checks Spark feature engineering logic.
- The build step publishes the Spark and Airflow images used by the batch feature pipeline.
- The deploy step updates the cluster so Airflow can launch the new Spark batch materialization code.

Image proof:

![DP2 CI/CD success](../../pngs/cicd_dp2.png)

### DP3 - ML Training Dataset Preparation

Code reference:

- [Jenkinsfile line 8](../../../Jenkinsfile#L8): defines the `dp3` CI/CD component.
- [jenkins/scripts/component_ci.sh line 127](../../../jenkins/scripts/component_ci.sh#L127): runs training dataset preparation tests.
- [jenkins/scripts/component_build_publish.sh line 121](../../../jenkins/scripts/component_build_publish.sh#L121): builds Spark, dataflow CLI, and Airflow images.
- [jenkins/scripts/component_deploy.sh line 104](../../../jenkins/scripts/component_deploy.sh#L104): deploys images used by DP3.
- [apps/ml-system/src/cli/prepare_bst_training_data.py line 278](../../../apps/ml-system/src/cli/prepare_bst_training_data.py#L278): reads offline feature data for training preparation.

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh dp3
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" PUBLISH_IMAGES=1 bash jenkins/scripts/component_build_publish.sh dp3
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" bash jenkins/scripts/component_deploy.sh dp3
```

Description of output when running command:

- The CI step validates the code that converts offline feature tables into ML-ready training rows.
- The build step publishes the runtime images used by dataset preparation.
- The deploy step updates Airflow/Spark/dataflow runtimes so the training dataset pipeline uses the latest code.

Image proof:

![DP3 CI/CD success](../../pngs/cicd_dp3.png)

## CI/CD For API

### Web API With FastAPI

Code reference:

- [Jenkinsfile line 9](../../../Jenkinsfile#L9): defines the `api` CI/CD component.
- [jenkins/scripts/component_ci.sh line 133](../../../jenkins/scripts/component_ci.sh#L133): runs FastAPI unit and contract tests.
- [jenkins/scripts/component_build_publish.sh line 126](../../../jenkins/scripts/component_build_publish.sh#L126): builds the FastAPI image.
- [jenkins/scripts/component_deploy.sh line 34](../../../jenkins/scripts/component_deploy.sh#L34): deploys the serving Helm chart.
- [infra/helm/recsys-serving/templates/api-deployment.yaml line 34](../../../infra/helm/recsys-serving/templates/api-deployment.yaml#L34): uses the deployed API image.
- [jenkins/post-deploy-e2e/Jenkinsfile line 16](../../../jenkins/post-deploy-e2e/Jenkinsfile#L16): runs post-deploy API verification.

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh api
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" PUBLISH_IMAGES=1 bash jenkins/scripts/component_build_publish.sh api
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" bash jenkins/scripts/component_deploy.sh api
bash jenkins/scripts/post_deploy_e2e.sh
```

Description of output when running command:

- The CI step should show FastAPI tests passing.
- The build step should create a commit-tagged API image.
- The deploy step should show Helm upgrade and `kubectl rollout status` for the API deployment.
- The post-deploy step should call `/health`, `/ready`, `/version`, `/recommendations`, and metrics endpoints.

Image proof:

![FastAPI CI/CD success](../../pngs/cicd_api_fastapi.png)

### Inference Engine With KServe

Code reference:

- [Jenkinsfile line 10](../../../Jenkinsfile#L10): defines the `kserve` CI/CD component.
- [jenkins/scripts/component_ci.sh line 139](../../../jenkins/scripts/component_ci.sh#L139): runs inference/KServe tests.
- [jenkins/scripts/component_build_publish.sh line 129](../../../jenkins/scripts/component_build_publish.sh#L129): documents that KServe consumes promoted model artifacts instead of building an app image.
- [jenkins/scripts/component_deploy.sh line 72](../../../jenkins/scripts/component_deploy.sh#L72): deploys KServe through model CD.
- [jenkins/scripts/model_cd.py line 44](../../../jenkins/scripts/model_cd.py#L44): reads the model promotion manifest.
- [jenkins/scripts/model_cd.py line 52](../../../jenkins/scripts/model_cd.py#L52): verifies required Triton model repository files.
- [jenkins/scripts/model_cd.py line 148](../../../jenkins/scripts/model_cd.py#L148): writes Helm values for the promoted model.
- [jenkins/scripts/model_cd.py line 207](../../../jenkins/scripts/model_cd.py#L207): applies the KServe deployment and waits for readiness.
- [infra/helm/recsys-serving/templates/inferenceservice.yaml line 28](../../../infra/helm/recsys-serving/templates/inferenceservice.yaml#L28): deploys the Triton `InferenceService` from `storageUri`.

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

export PROMOTION_MANIFEST_URI="s3://recsys-model-store/promotions/bst/production.json"

COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh kserve
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" PUBLISH_IMAGES=1 bash jenkins/scripts/component_build_publish.sh kserve
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" bash jenkins/scripts/component_deploy.sh kserve
```

Description of output when running command:

- The CI step validates KServe/inference integration.
- The build step records that KServe will consume a production model artifact instead of a normal application image.
- The deploy step reads the production promotion manifest, verifies the Triton model repository files, writes serving Helm values, upgrades KServe, and waits until the `InferenceService` is ready.

Image proof:

![KServe model CD success](../../pngs/cicd_kserve_model_cd.png)

### Real-Time Drift Detection Web API

Code reference:

- [Jenkinsfile line 11](../../../Jenkinsfile#L11): defines the `drift` CI/CD component.
- [jenkins/scripts/component_ci.sh line 145](../../../jenkins/scripts/component_ci.sh#L145): runs drift detection tests.
- [jenkins/scripts/component_build_publish.sh line 132](../../../jenkins/scripts/component_build_publish.sh#L132): builds the dataflow CLI image used by drift detection.
- [jenkins/scripts/component_deploy.sh line 80](../../../jenkins/scripts/component_deploy.sh#L80): deploys drift runtime and applies Knative manifests when present.
- [apps/data-platform/src/validate/offline_feature_drift.py line 83](../../../apps/data-platform/src/validate/offline_feature_drift.py#L83): computes PSI drift metrics.
- [apps/data-platform/src/mlops/trigger_kubeflow_retrain.py line 95](../../../apps/data-platform/src/mlops/trigger_kubeflow_retrain.py#L95): triggers retraining when drift policy is breached.

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh drift
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" PUBLISH_IMAGES=1 bash jenkins/scripts/component_build_publish.sh drift
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" bash jenkins/scripts/component_deploy.sh drift
```

Description of output when running command:

- The CI step verifies drift metric calculation and retrain trigger logic.
- The build step publishes the dataflow runtime image used by the drift detection service/job.
- The deploy step updates the data platform drift runtime and applies the Knative/KServe eventing manifests if the drift service manifests are available.

Image proof:

![Realtime drift CI/CD success](../../pngs/cicd_realtime_drift.png)

## CI/CD For Jobs

### Job 1 - Push Stream Feature To Offline Store

Code reference:

- [Jenkinsfile line 12](../../../Jenkinsfile#L12): defines the `stream_offline` CI/CD component.
- [jenkins/scripts/component_ci.sh line 152](../../../jenkins/scripts/component_ci.sh#L152): runs streaming offline sink tests.
- [jenkins/scripts/component_build_publish.sh line 135](../../../jenkins/scripts/component_build_publish.sh#L135): builds the Flink image.
- [jenkins/scripts/component_deploy.sh line 110](../../../jenkins/scripts/component_deploy.sh#L110): deploys the Flink stream job image.
- [infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml line 39](../../../infra/helm/recsys-data-platform/templates/realtime-flink-consumer.yaml#L39): enables the offline-store sink.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 526](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L526): builds offline feature rows.

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh stream_offline
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" PUBLISH_IMAGES=1 bash jenkins/scripts/component_build_publish.sh stream_offline
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" bash jenkins/scripts/component_deploy.sh stream_offline
```

Description of output when running command:

- The CI step validates the streaming offline feature sink.
- The build step publishes the Flink image.
- The deploy step updates the realtime Flink consumer so streaming features are pushed into the Iceberg offline feature store.

Image proof:

![Stream offline job CI/CD success](../../pngs/cicd_stream_offline.png)

### Job 2 - Push Stream Feature To Online Store

Code reference:

- [Jenkinsfile line 13](../../../Jenkinsfile#L13): defines the `stream_online` CI/CD component.
- [jenkins/scripts/component_ci.sh line 158](../../../jenkins/scripts/component_ci.sh#L158): runs streaming online sink tests.
- [jenkins/scripts/component_build_publish.sh line 138](../../../jenkins/scripts/component_build_publish.sh#L138): builds Flink and dataflow CLI images.
- [jenkins/scripts/component_deploy.sh line 113](../../../jenkins/scripts/component_deploy.sh#L113): deploys the Flink stream job and online writer runtime.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 483](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L483): writes online features to Redis.
- [apps/data-platform/src/features/flink/realtime_stream_job.py line 735](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L735): names the online sink `redis-online-feature-writer`.

Running command:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

COVERAGE_MIN=90 bash jenkins/scripts/component_ci.sh stream_online
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" PUBLISH_IMAGES=1 bash jenkins/scripts/component_build_publish.sh stream_online
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" bash jenkins/scripts/component_deploy.sh stream_online
```

Description of output when running command:

- The CI step validates the Redis online feature writer.
- The build step publishes Flink and dataflow runtime images.
- The deploy step updates the realtime Flink consumer so streaming features are pushed into the Redis online feature store.

Image proof:

![Stream online job CI/CD success](../../pngs/cicd_stream_online.png)

## Proof Capture Checklist

- Jenkins pipeline screenshot: capture the build page with `Component CI`, `Build And Publish`, and `Deploy Or Update` green for each selected component.
- Jenkins console screenshot: capture test success, image tag, Helm upgrade, rollout status, or KServe readiness logs.
- Kubernetes verification screenshot when useful:

```bash
kubectl get pods -n ci
kubectl get deploy -n recsys-dataflow
kubectl get deploy -n api-serving
kubectl get inferenceservice -n kserve-triton-inference
kubectl get deploy realtime-flink-consumer -n recsys-dataflow
```
