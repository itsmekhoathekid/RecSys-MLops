# Jenkins Path-Based CI/CD

Root `Jenkinsfile` is the component-aware CI/CD entrypoint. It detects changed
paths, runs only the affected component gates, pushes only the affected images,
and updates only the affected deployed component. Pull-request branches run CI;
the merge commit on `main` publishes and deploys by default.

Serving mutation testing is intentionally isolated from the push/PR feedback
loop. The standalone [`ServingMutation.Jenkinsfile`](ServingMutation.Jenkinsfile)
runs nightly or manually, accepts an `inference`, `online-feature`, `rag`, or
`all` scope, and archives the target-scoped JSON/text score reports.

## GitHub Webhook Flow

The in-cluster Jenkins chart seeds a Pipeline-from-SCM job named
`RecSys-GitHub-CICD`. The job points to the GitHub repository and reads the root
`Jenkinsfile`; CI/CD behavior stays in source control instead of inside the
Jenkins UI.

```text
GitHub push (feature/PR branch or main)
  -> GitHub Webhook
  -> Jenkins /github-webhook/
  -> RecSys-GitHub-CICD job
  -> Jenkinsfile
  -> Detect Changed Components
  -> Component CI
  -> Component Build And Publish
  -> PR branch: stop after CI/build proof
  -> PR merged to main: publish image and Component Deploy Or Update
```

Webhook settings:

```text
Payload URL: https://<production-gateway>/github-webhook/
Content type: application/json
Required event: push
```

The Helm chart exposes only `/github-webhook/` through the ingress controller.
The root pipeline declares [`githubPush()`](../Jenkinsfile#L20-L22). Updating a
PR branch is a push to that branch; merging the PR creates a new push on
`main`. A separate `pull_request` delivery is not required by this job and
should not be used to queue a duplicate build for the same commit.

## Components

| Component | Trigger paths | Published artifacts |
| --- | --- | --- |
| `ci_config` | `Jenkinsfile`, `jenkins/`, `infra/helm/`, Terraform and `ops/` | None. Runs catalog/detector contracts, Python compile checks, shell syntax, and all Helm renders. |
| `materialize` | feature-store source, materialize DAG/config | `recsys-feature-store`, `recsys-airflow` |
| `training` | `apps/ml-system/`, `pipelines/kubeflow/`, `configs/ml-system/training/bst.yaml` | `recsys-mlops-training`, `recsys-spark`, `recsys-mlflow`, `recsys-drift-retrain`, compiled/uploaded KFP YAML |
| `dp1` | raw ingestion, data generator, source CDC config | `recsys-data-ingestion`, `recsys-spark`, `recsys-airflow`, `recsys-kafka-connect` |
| `dp2` | silver/gold Spark transforms and DAG/config | `recsys-spark`, `recsys-airflow` |
| `dp3` | offline feature builders and feature store config | `recsys-spark`, `recsys-feature-store`, `recsys-airflow` |
| `rag_api` | RAG API/runtime, exact-chunk and semantic retrieval contracts | `recsys-rag-api` |
| `online_feature_api` | shared/Feature source, Feature chart and contract tests | `recsys-online-feature-api` |
| `feature_rag_mcp` | stateless MCP facade, tool contract, image and Helm chart | `recsys-feature-rag-mcp`, MCP Registry metadata |
| `context_agent` | regular/sandbox agent chart, prompts and A2A tests | chart-only release, regular/sandbox Registry metadata |
| `inference_api` | shared/Inference source, gateway and recommendation tests | `recsys-inference-api` |
| `kserve` | `infra/helm/recsys-serving/`, `model_cd.py`, model promotion serving code | production model manifest update |
| `rollout` | rollout controller, Model-CD pipeline/script, watcher Helm resource, rollout load test, serving/observability contracts | immutable `recsys-mlops-training` watcher image and updated watcher Deployment |
| `drift` | `validate/`, `mlops/`, drift DAG and observability manifests | `recsys-drift-retrain`, `recsys-airflow` |
| `stream_offline` | Flink stream jobs and Iceberg sink code | `recsys-flink` |
| `stream_online` | Flink stream jobs, Redis/online writer code | `recsys-flink` |
| `analytics` | `apps/analytics/`, analytics tests, Airflow analytics DAG, and `infra/helm/recsys-analytics/` | `recsys-spark`, `recsys-analytics-dbt`, `recsys-analytics-superset`, `recsys-airflow` |
| `demo_web` | demo frontend/backend, chart, security and smoke test | `recsys-demo-api`, `recsys-demo-web` |

`jenkins/config/components.json` is the source of truth for path
rules. `jenkins/python/change_detection/detector.py` evaluates those rules, and
`jenkins/python/release_plan.py` resolves image, artifact, and deploy-unit fan-out.
They write `.ci-components.env` and `.ci-release-plan.json` so Jenkins can run
the matching component stages while building each image and deploying each
release only once.

Documentation and generated evidence paths (`docs/`, Markdown, images,
`graphify-out/`, CI reports) are explicitly ignored and produce
`CHANGED_COMPONENTS=unchanged`. Jenkins/controller configuration produces only
`RUN_CI_CONFIG=true` without inventing a product component; it no longer fans
out to every application pipeline. Any non-documentation path without a routing rule fails closed with an
`Unmapped runtime path` error so a new component cannot silently run everything
or skip validation.

Analytics changes set `RUN_ANALYTICS=true` in the webhook flow. The shared
pipeline runs analytics unit and contract tests plus Helm validation, publishes
the unified Spark, dbt, Superset, and Airflow images, and upgrades the
independently owned Airflow and analytics Helm releases after CI passes.

Progressive rollout changes set `RUN_ROLLOUT=true`. The same main webhook flow
tests the controller, model-CD contracts, Helm templates, and demo scripts;
publishes the watcher image with the Git commit tag; and applies only the
watcher Deployment. The deploy step runs the idempotent Jenkins seed
through `/scriptText`, updating rollout jobs without restarting the Jenkins
controller. `RecSys-KServe-Model-CD` is Pipeline-from-SCM, so every runtime rollout
stage checks out `jenkins/KServeModelCD.Jenkinsfile` and its scripts from the
current main revision instead of using a manually synchronized workspace.

For push builds, Jenkins compares `GIT_PREVIOUS_COMMIT...HEAD`. Pull requests use
the target branch merge base. `HEAD~1` is only a first-build fallback. This makes
multi-commit pushes and repeated builds deterministic while preserving a valid
empty diff as unchanged.

A documentation-only push is expected to log `Changed components: unchanged`;
CI configuration validation, product CI, image publishing, and deployment must
all remain skipped for that build.

## Async Serving CI References

The async serving refactor is routed and tested as five independent components:

| Component | Runtime reference | CI reference | Helm reference |
| --- | --- | --- | --- |
| `online_feature_api` | [`service.py`](../apps/api-serving/online-feature-api/src/recsys_online_feature_api/service.py) | [`ci_online_feature_api`](scripts/ci/serving.sh) | [`recsys-online-feature-api`](../infra/helm/recsys-online-feature-api/) |
| `inference_api` | [`triton.py`](../apps/api-serving/inference-api/src/recsys_inference_api/triton.py), [`shadow.py`](../apps/api-serving/inference-api/src/recsys_inference_api/shadow.py) | [`ci_inference_api`](scripts/ci/serving.sh) | [`recsys-inference-api`](../infra/helm/recsys-inference-api/) |
| `rag_api` | [`app.py`](../apps/api-serving/rag-api/src/recsys_rag_api/app.py) | [`ci_rag_api`](scripts/ci/serving.sh) | [`recsys-rag-api`](../infra/helm/recsys-rag-api/) |
| `feature_rag_mcp` | [`app.py`](../apps/agentic/recsys-feature-rag-mcp/src/recsys_feature_rag_mcp/app.py) | [`agentic.sh`](scripts/ci/agentic.sh) | [`recsys-feature-rag-mcp`](../infra/helm/recsys-feature-rag-mcp/) |
| `demo_web` | [`database.py`](../apps/demo-web/backend/app/database.py) | [`demo.sh`](scripts/ci/demo.sh) | [`recsys-demo-web`](../infra/helm/recsys-demo-web/) |

Shared limiter behavior and its cancellation/timeout regression coverage are in
[`concurrency.py`](../apps/api-serving/shared/src/recsys_serving_common/concurrency.py)
and [`test_concurrency.py`](../tests/unit/api_serving/test_concurrency.py). The
exact path ownership and CI profiles remain authoritative in
[`components.json`](config/components.json) and
[`ci-environments.json`](config/ci-environments.json).

## Agentic production gate

The three agent components are Context, Recommendation, and Coordinator
`SandboxAgent`s backed by independent Substrate WorkerPools. Coordinator v21
sets `isolateSessions: true` on both specialist tools; Recommendation v9 copies
the current A2A arguments exactly and stops after its single MCP response.
Specialist smokes use a 600-second single attempt and the six-case Coordinator
suite uses a 1,800-second single attempt. A client timeout must never trigger a
whole-suite retry because the A2A task can still be running server-side.

Run their dependency-closed production release with:

```text
FORCE_COMPONENTS=feature_rag_mcp,context_agent,recommendation_mcp,recommendation_agent,coordinator_agent,ci_config
FORCE_DEPLOY=true
```

## Stage Contract

Each changed component follows the same sequence:

1. Component unit tests with `pytest-cov` and `COVERAGE_MIN`, default `90`.
2. Component integration tests from `tests/integration/<component>/` when present.
3. Existing contract tests relevant to the component.
4. Docker build with the full `GIT_COMMIT` tag.
5. Push to the production Artifact Registry and resolve an immutable digest.
6. Deploy the digest to GKE for `main`, including PR merge commits. An unmerged
   PR requires `DEPLOY_PULL_REQUESTS=true` or the one-run `FORCE_DEPLOY=true`.
7. Run component production smoke/workflow checks after all selected release
   layers are healthy.

Component CI is executed in bounded batches. The default maximum is three
parallel branches (`COMPONENT_CI_MAX_PARALLEL=3`) so the Jenkins controller pod
does not launch all thirteen heavy Python/Node test processes at once.

The RAG post-promotion recall gate is catalog-coverage aware. The configured
full-catalog target remains `0.90`; the verifier multiplies it by
`indexed_item_count / golden_catalog_item_count` when a complete reduced corpus
is intentionally promoted. For the current 96/160 quota-fallback corpus the
effective target is `0.54`. The evidence report records both thresholds and the
coverage ratio, while latency, duplicate-response and hard-constraint gates are
never relaxed.

`jenkins/config/gcp-production.json` is the only production identity source.
The Jenkins job has no project, registry, cluster, or kubeconfig override.
Services pull `@sha256:` references; the pipeline does not deploy mutable tags.

The `training` component has an extra Kubeflow package gate: CI/CD builds and
pushes `recsys-mlops-training`, `recsys-spark`, and `recsys-mlflow`,
compiles `pipelines/kubeflow/compiled/bst_training_pipeline.yaml` with those real
image refs, validates and archives it, then uploads or versions the package in
Kubeflow. Runtime retrain jobs submit the uploaded pipeline by name/version and
do not read the generated YAML from a container filesystem.

Each Helm unit uses `upgrade --install --atomic --cleanup-on-fail --wait` and has
its own Jenkins lock. The release plan topologically orders dependencies and
parallelizes only independent releases. A failed Helm unit rolls back its own
revision; Jenkins stops later layers and does not run production workflow checks.
Kubeflow package upload, Feast registry apply, and rollout reconciliation are
explicit non-Helm deploy units in the same plan.

Production verification has a separate component dependency graph. A full run
checks DP1, DP2, DP3, materialization and training in that order regardless of
the order supplied in `FORCE_COMPONENTS`.

## Secrets

Jenkins runs as `ci/recsys-jenkins` and obtains Artifact Registry credentials
through Workload Identity Federation for GKE. Do not configure a static GCP
service-account JSON key. MinIO/S3, MLflow and model registry credentials are
loaded from Kubernetes Secrets only when model CD needs them.

Production application charts set `secret.create=false`. Terraform writes
central payloads in `external-secrets`, and the Terraform-owned
`recsys-security` release creates namespace-local ExternalSecrets. Jenkins
verifies each required ExternalSecret and target Secret before upgrading a
dependent release; it never renders secret values into Helm arguments.

Do not commit secret values into Jenkinsfile, Helm values, or scripts.

## Full Service CI/CD

Run the root Jenkins job with `FORCE_DEPLOY=true` and the complete component
list in `FORCE_COMPONENTS`:

```text
materialize,training,dp1,dp2,dp3,datahub_catalog,online_feature_api,inference_api,kserve,rollout,drift,stream_offline,stream_online,analytics,demo_web,rag_index,rag_api,ci_config
```

The root pipeline keeps a compact Stage View: Declarative checkout, Checkout,
Detect Changed Components, Python Env, Component CI, Docker Login, Component
Build And Publish, Component Deploy Or Update, and Declarative post actions.
Component CI still runs selected branches in parallel. Build/package and
preflight/deploy/verification retain their internal checkpoints and release
locks, but no longer create one UI column per checkpoint. Post-deploy
verification checks only applied resources, health endpoints, registered DAGs,
and uploaded KFP packages; it never starts Airflow DAGs, KFP runs, Spark jobs,
or synthetic production events.

Run the local preflight, then trigger the job through the Jenkins API:

```bash
make full-cicd-preflight

JENKINS_URL=https://jenkins.example.com \
JENKINS_USER=admin \
JENKINS_TOKEN="$JENKINS_TOKEN" \
make jenkins-full
```

Set `DATAHUB_CUTOVER_MODE=plan` to archive a cleanup manifest, or `apply` to
pause at the Jenkins approval gate and then apply that exact reviewed manifest.

Set `COMPONENT_CI_MAX_PARALLEL=2` on smaller controller pods. The trigger always
publishes images and requests production deployment; use the Jenkins
`PUBLISH_IMAGES=false` parameter only for a non-deploying build proof.
