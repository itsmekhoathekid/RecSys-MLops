# Jenkins Path-Based CI/CD

Root `Jenkinsfile` is the component-aware CI/CD entrypoint. It detects changed
paths, runs only the affected component gates, pushes only the affected images,
and updates only the affected deployed component on `main`.

## GitHub Webhook Flow

The in-cluster Jenkins chart seeds a Pipeline-from-SCM job named
`RecSys-GitHub-CICD`. The job points to the GitHub repository and reads the root
`Jenkinsfile`; CI/CD behavior stays in source control instead of inside the
Jenkins UI.

```text
GitHub push/PR
  -> GitHub Webhook
  -> Jenkins /github-webhook/
  -> RecSys-GitHub-CICD job
  -> Jenkinsfile
  -> Detect Changed Components
  -> Component CI
  -> Component Build And Publish
  -> Component Deploy Or Update only when branch is main
```

Webhook settings:

```text
Payload URL on GKE proof cluster: http://34.21.171.234/github-webhook/
Content type: application/json
Events: push and pull_request
```

The Helm chart exposes only `/github-webhook/` through the ingress controller.
Use port-forward for the Jenkins UI:

```bash
kubectl port-forward -n ci svc/recsys-jenkins 18090:8080
```

## Components

| Component | Trigger paths | Published artifacts |
| --- | --- | --- |
| `ci_config` | `Jenkinsfile`, `.github/`, `jenkins/`, `infra/helm/recsys-ci/`, generic IaC/control files | None. Runs detector contracts, Python compile checks, and Jenkins Helm lint only. |
| `materialize` | `feature_store/`, `local/`, materialize DAG/config | `recsys-dataflow-cli` |
| `training` | `apps/ml-system/`, `infra/kubeflow/`, `configs/local/bst.yaml` | `recsys-mlops-training`, `recsys-mlops-spark`, `recsys-dataflow-cli`, compiled/uploaded KFP YAML |
| `spark_batch` | `features/spark/`, `Dockerfile.spark`, `spark_batch*.yaml` | `recsys-spark`, `recsys-airflow` |
| `dp1` | raw ingestion, data generator, source CDC config | `recsys-data-generator`, `recsys-dataflow-cli`, `recsys-airflow`, `recsys-kafka-connect` |
| `dp2` | silver/gold Spark transforms and DAG/config | `recsys-spark`, `recsys-airflow` |
| `dp3` | offline feature builders and feature store config | `recsys-spark`, `recsys-dataflow-cli`, `recsys-airflow` |
| `api` | `apps/api-serving/`, API tests, serving chart | `recsys-api-serving` |
| `kserve` | `infra/helm/recsys-serving/`, `model_cd.py`, model promotion serving code | production model manifest update |
| `drift` | `validate/`, `mlops/`, future Knative/KServe drift manifests | `recsys-dataflow-cli` |
| `stream_offline` | Flink stream jobs and Iceberg sink code | `recsys-flink` |
| `stream_online` | Flink stream jobs, Redis/online writer code | `recsys-flink`, `recsys-dataflow-cli` |

`jenkins/scripts/detect_changed_components.py` is the source of truth for path
classification. It writes `.ci-components.env` so Jenkins can run the matching
component stages.

Documentation and generated evidence paths (`docs/`, Markdown, images,
`graphify-out/`, CI reports) are explicitly ignored and produce
`CHANGED_COMPONENTS=unchanged`. Jenkins/controller configuration produces only
`CHANGED_COMPONENTS=ci_config`; it no longer fans out to every application
pipeline. Any non-documentation path without a routing rule fails closed with an
`Unmapped runtime path` error so a new component cannot silently run everything
or skip validation.

For push builds, Jenkins compares `GIT_PREVIOUS_COMMIT...HEAD`. Pull requests use
the target branch merge base. `HEAD~1` is only a first-build fallback. This makes
multi-commit pushes and repeated builds deterministic while preserving a valid
empty diff as unchanged.

## Stage Contract

Each changed component follows the same sequence:

1. Component unit tests with `pytest-cov` and `COVERAGE_MIN`, default `90`.
2. Component integration tests from `tests/integration/<component>/` when present.
3. Existing contract tests relevant to the component.
4. Docker build and immutable image tag with `GIT_COMMIT`.
5. Optional push to `IMAGE_PUSH_REGISTRY`, default `localhost:5001/recsys`.
6. Deploy/update only on `main` unless `FORCE_DEPLOY=true`.

Services should pull the pushed registry image in CI/CD. The pipeline does not
deploy `*:local` tags.

The `training` component has an extra Kubeflow package gate: CI/CD builds and
pushes `recsys-mlops-training`, builds and pushes `recsys-mlops-spark`,
compiles `infra/kubeflow/compiled/bst_training_pipeline.yaml` with those real
image refs, validates the package contains no `:local` token, uploads or
versions the package in Kubeflow, and rolls the `recsys-dataflow-cli` trigger
runtime so drift retrain pods submit the same package.

For the in-cluster Jenkins setup in `infra/helm/recsys-ci`, Jenkins pushes to
`recsys-registry.ci.svc.cluster.local:5000/recsys`, while workloads pull from
`localhost:5001/recsys` through the registry node proxy. The registry itself is
backed by a PVC inside the cluster.

## Secrets

Keep secrets in Jenkins credentials or injected environment variables:

- `REGISTRY_CREDENTIALS_ID`: optional username/password for `docker login`.
- `KUBECONFIG_CREDENTIALS_ID`: optional kubeconfig file credential for deploy.
- MinIO/S3, MLflow, and model registry credentials for KServe/model CD should be
  injected by Jenkins as environment variables consumed by `model_cd.py`.

Do not commit secret values into Jenkinsfile, Helm values, or scripts.

## Post-CD E2E

Full service E2E is intentionally separate from the main CI/CD pipeline.

Use `jenkins/post-deploy-e2e/Jenkinsfile` or run:

```bash
jenkins/scripts/post_deploy_e2e.sh
```

The post-CD job does not build, push, or deploy. It verifies already-running
services: FastAPI, KServe, Spark/data outputs, stream offline store, Redis online
store, drift/metrics, and observability smoke checks.

## Full Service CI/CD

To force every RecSys service through CI, image publish, deploy, and E2E gates:

```bash
IMAGE_REGISTRY=asia-southeast1-docker.pkg.dev/fsds-coursework/recsys \
IMAGE_TAG=full-cicd-YYYYMMDD-rN \
FULL_CICD_BUILD_BACKEND=cloudbuild \
jenkins/scripts/full_services_cicd.sh
```

The full flow runs component CI, builds/pushes all runtime images, compiles and
validates the Kubeflow package with pullable image refs, uploads the package,
deploys data platform, MLflow, API, and KServe/model CD, then runs data-platform,
ML-platform, and post-deploy E2E checks.

## Validation Evidence

Rubric evidence for API validation can be generated after component CI:

```bash
COVERAGE_MIN=90 UV_CACHE_DIR=.uv-cache bash jenkins/scripts/component_ci.sh api
MUTATION_TARGETS='apps/api-serving/src/ranking.py apps/api-serving/src/online_features.py' MUTATION_MUTANT_NAMES='ranking.x_format_top_k* online_features.x_get_online_features*' UV_CACHE_DIR=.uv-cache bash jenkins/scripts/validation_mutation.sh
RECSYS_LOAD_HOST=http://127.0.0.1:8088 UV_CACHE_DIR=.uv-cache bash jenkins/scripts/validation_load_test.sh
bash jenkins/scripts/validation_evidence.sh
```

`validation_mutation.sh` accepts `MUTATION_TARGETS` or derives changed Python
source files from `MUTATION_BASE_REF`, then limits mutmut to those files.
`MUTATION_MUTANT_NAMES` can narrow the run further to changed functions. The
Locust SLA gate is `0%` failures, p95 `<1000ms`, and throughput `>=5 req/s`.
Submission proof is written under
`docs/submission/rubic-final-coursework-(final-ml)/validation-verification/`.
