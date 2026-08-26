# Submission Documentation Reference Guide

This folder contains coursework explanations, source-code references, and
captured runtime evidence. It was last audited against the current `main`
branch and live GCP agent stack on 26 August 2026.

## How To Read References

- A relative repository link points to the current production source or
  configuration.
- A GitHub URL pinned to a full commit SHA points to historical proof that was
  intentionally removed or superseded. It should not be treated as a runnable
  path in the current checkout.
- Screenshots, run IDs, metrics, timestamps, and command output describe a
  captured evidence run. Re-run the associated current command before using
  them as a statement about live cluster state.

## Current Production Sources

| Concern | Authoritative source |
|---|---|
| Container image catalog | [`images/catalog.json`](../../images/catalog.json) |
| CI component detection | [`jenkins/config/components.json`](../../jenkins/config/components.json) |
| CD deploy units | [`jenkins/config/deploy-units.json`](../../jenkins/config/deploy-units.json) |
| Release build and deployment | [`jenkins/scripts/entrypoints/release_build_publish.sh`](../../jenkins/scripts/entrypoints/release_build_publish.sh), [`release_deploy_unit.sh`](../../jenkins/scripts/entrypoints/release_deploy_unit.sh) |
| Async FastAPI serving runtime | [`apps/api-serving/README.md`](../../apps/api-serving/README.md), [`concurrency.py`](../../apps/api-serving/shared/src/recsys_serving_common/concurrency.py) |
| Serving CI/CD routing and gates | [`Jenkinsfile`](../../Jenkinsfile), [`components.json`](../../jenkins/config/components.json), [`serving.sh`](../../jenkins/scripts/ci/serving.sh) |
| Data-platform configuration | [`infra/helm/recsys-data-config`](../../infra/helm/recsys-data-config) |
| Lakehouse, source, stream, feature-store, and orchestration releases | [`infra/helm`](../../infra/helm) split charts |
| Generator scenarios | [`configs/data-platform/generator`](../../configs/data-platform/generator) |
| Spark DP2 and DP3 entrypoints | [`dp2_silver_gold_entrypoint.py`](../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py), [`dp3_offline_feature_entrypoint.py`](../../apps/data-platform/src/features/spark/dp3_offline_feature_entrypoint.py) |
| Kubeflow package compilation/upload | [`kfp_package.sh`](../../jenkins/scripts/build/kfp_package.sh), [`upload_kfp_package.sh`](../../jenkins/scripts/deploy/upload_kfp_package.sh) |
| GCP infrastructure | [`infra/terraform/gcp`](../../infra/terraform/gcp) |
| Global kagent model configuration | [`configs/kagent/values.yaml`](../../configs/kagent/values.yaml) |
| Context and recommendation specialist Agents | [`recsys-kagent-agent`](../../infra/helm/recsys-kagent-agent), [`recsys-recommendation-agent`](../../infra/helm/recsys-recommendation-agent) |
| Regular coordinator Agent | [`recsys-coordinator-agent`](../../infra/helm/recsys-coordinator-agent) |
| Current agent rollout/rollback evidence | [`validation_verification.md`](<rubric-final-coursework-(final-llm)/validation_verification.md>) |

The data platform is no longer one Helm release. Production ownership is split
across `recsys-data-config`, `recsys-data-lakehouse`,
`recsys-source-store`, `recsys-event-stream`, `recsys-feature-store`,
`recsys-kafka-connect`, `recsys-streaming`, and `recsys-airflow`.

The current agent baseline is kagent `0.9.9` with Substrate `0.0.6` for the two
specialist WorkerPools and CPU-based KEDA. The coordinator is a regular kagent
`Agent` fixed at one replica. Substrate `0.0.11` assigned-worker scaling is
canary evidence only: production failed the A2A compatibility gate and was
rolled back. Historical screenshots and output remain evidence of their stated
run, but do not override this baseline.
