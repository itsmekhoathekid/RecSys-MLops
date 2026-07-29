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
| `materialize` | `feature_store/`, `local/`, materialize DAG/config | `recsys-feature-store` |
| `training` | `apps/ml-system/`, `infra/kubeflow/`, `configs/local/bst.yaml` | `recsys-mlops-training`, `recsys-mlops-spark`, `recsys-drift-retrain`, compiled/uploaded KFP YAML |
| `spark_batch` | `features/spark/`, `Dockerfile.spark`, `spark_batch*.yaml` | `recsys-spark`, `recsys-airflow` |
| `dp1` | raw ingestion, data generator, source CDC config | `recsys-data-ingestion`, `recsys-spark`, `recsys-airflow`, `recsys-kafka-connect` |
| `dp2` | silver/gold Spark transforms and DAG/config | `recsys-spark`, `recsys-airflow` |
| `dp3` | offline feature builders and feature store config | `recsys-spark`, `recsys-feature-store`, `recsys-airflow` |
| `api` | `apps/api-serving/`, API tests, serving chart | `recsys-api-serving` |
| `kserve` | `infra/helm/recsys-serving/`, `model_cd.py`, model promotion serving code | production model manifest update |
| `rollout` | rollout controller, Model-CD pipeline/script, watcher Helm resource, rollout load test, serving/observability contracts | immutable `recsys-mlops-training` watcher image and updated watcher Deployment |
| `drift` | `validate/`, `mlops/`, future Knative/KServe drift manifests | `recsys-drift-retrain` |
| `stream_offline` | Flink stream jobs and Iceberg sink code | `recsys-flink` |
| `stream_online` | Flink stream jobs, Redis/online writer code | `recsys-flink` |
| `analytics` | `apps/analytics/`, analytics tests, Airflow analytics DAG, and `infra/helm/recsys-analytics/` | `recsys-analytics-spark`, `recsys-analytics-dbt`, `recsys-analytics-superset`, `recsys-airflow` |

`jenkins/python/change_detection/detector.py` is the source of truth for path
classification. It writes `.ci-components.env` so Jenkins can run the matching
component stages.

Documentation and generated evidence paths (`docs/`, Markdown, images,
`graphify-out/`, CI reports) are explicitly ignored and produce
`CHANGED_COMPONENTS=unchanged`. Jenkins/controller configuration produces only
`CHANGED_COMPONENTS=ci_config`; it no longer fans out to every application
pipeline. Any non-documentation path without a routing rule fails closed with an
`Unmapped runtime path` error so a new component cannot silently run everything
or skip validation.

Analytics changes set `RUN_ANALYTICS=true` in the main webhook flow. The shared
pipeline runs analytics unit and contract tests plus Helm validation, publishes
the Spark, dbt, Superset, and Airflow images, and upgrades the data-platform and
analytics Helm releases on `main`. Jenkins also seeds the manual proof job
`RecSys-Analytics-BI-CICD` under the `10 Analytics And BI` view; that job reuses
the same root `Jenkinsfile` with `FORCE_COMPONENTS=analytics` rather than
duplicating the production pipeline definition.

Progressive rollout changes set `RUN_ROLLOUT=true`. The same main webhook flow
tests the controller, model-CD contracts, Helm templates, and demo scripts;
publishes the watcher image with the Git commit tag; and applies only the
watcher Deployment on `main`. The deploy step runs the idempotent Jenkins seed
through `/scriptText`, updating rollout jobs without restarting the Jenkins
controller. Jenkins also seeds
`RecSys-Progressive-Rollout-CICD` in `06B Progressive Model Rollout` for manual
proof. `RecSys-KServe-Model-CD` is Pipeline-from-SCM, so every runtime rollout
stage checks out `jenkins/KServeModelCD.Jenkinsfile` and its scripts from the
current main revision instead of using a manually synchronized workspace.

For push builds, Jenkins compares `GIT_PREVIOUS_COMMIT...HEAD`. Pull requests use
the target branch merge base. `HEAD~1` is only a first-build fallback. This makes
multi-commit pushes and repeated builds deterministic while preserving a valid
empty diff as unchanged.

A documentation-only push is expected to log `Changed components: unchanged`;
CI configuration validation, product CI, image publishing, and deployment must
all remain skipped for that build.

## Stage Contract

Each changed component follows the same sequence:

1. Component unit tests with `pytest-cov` and `COVERAGE_MIN`, default `90`.
2. Component integration tests from `tests/integration/<component>/` when present.
3. Existing contract tests relevant to the component.
4. Docker build and vulnerability scan with the full `GIT_COMMIT` tag.
5. Push to the production Artifact Registry and resolve an immutable digest.
6. Deploy the digest to GKE only on `main`, unless `FORCE_DEPLOY=true`.
7. Run the component production test before committing its transaction.

`jenkins/config/gcp-production.json` is the only production identity source.
The Jenkins job has no project, registry, cluster, or kubeconfig override.
Services pull `@sha256:` references; the pipeline does not deploy mutable tags.

The `training` component has an extra Kubeflow package gate: CI/CD builds and
pushes `recsys-mlops-training`, `recsys-mlops-spark`, and `recsys-mlflow`,
compiles `infra/kubeflow/compiled/bst_training_pipeline.yaml` with those real
image refs, validates the package contains no `:local` token, uploads or
versions the package in Kubeflow, and rolls the `recsys-drift-retrain` trigger
runtime so drift retrain pods submit the same package.

Each component deploy is a durable Saga stored below
`$JENKINS_HOME/ci-transactions`. It snapshots Helm revisions, effective values,
workload digests, KFP versions, model-store VersionIds, Jenkins job XML and
migration state. A failed deploy or production test compensates external state,
rolls Helm back, verifies old digests/readiness, and finishes as `ROLLED_BACK`.
`ROLLBACK_FAILED` blocks the component until an operator repairs it.

## Secrets

Jenkins runs as `ci/recsys-jenkins` and obtains Artifact Registry credentials
through Workload Identity Federation for GKE. Do not configure a static GCP
service-account JSON key. MinIO/S3, MLflow and model registry credentials are
loaded from Kubernetes Secrets only when model CD needs them.

Do not commit secret values into Jenkinsfile, Helm values, or scripts.

## Full Service CI/CD

Run the root Jenkins job with `FORCE_DEPLOY=true` and the complete component
list in `FORCE_COMPONENTS`:

```text
materialize,training,spark_batch,dp1,dp2,dp3,api,kserve,rollout,drift,stream_offline,stream_online,analytics,demo_web,ci_config
```

The root pipeline keeps the normal Stage View while every component runs its
own locked CI environment, image publish, atomic deploy, production test, and
rollback transaction. There is no separate legacy full-service script or
post-commit data/ML flow.
