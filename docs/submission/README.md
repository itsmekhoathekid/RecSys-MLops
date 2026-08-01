# Submission Documentation Reference Guide

This folder contains coursework explanations, source-code references, and
captured runtime evidence. It was audited against the current `main` branch on
1 August 2026.

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
| Data-platform configuration | [`infra/helm/recsys-data-config`](../../infra/helm/recsys-data-config) |
| Lakehouse, source, stream, feature-store, and orchestration releases | [`infra/helm`](../../infra/helm) split charts |
| Generator scenarios | [`configs/data-platform/generator`](../../configs/data-platform/generator) |
| Spark DP2 and DP3 entrypoints | [`dp2_silver_gold_entrypoint.py`](../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py), [`dp3_offline_feature_entrypoint.py`](../../apps/data-platform/src/features/spark/dp3_offline_feature_entrypoint.py) |
| Kubeflow package compilation/upload | [`kfp_package.sh`](../../jenkins/scripts/build/kfp_package.sh), [`upload_kfp_package.sh`](../../jenkins/scripts/deploy/upload_kfp_package.sh) |
| GCP infrastructure | [`infra/terraform/gcp`](../../infra/terraform/gcp) |

The data platform is no longer one Helm release. Production ownership is split
across `recsys-data-config`, `recsys-data-lakehouse`,
`recsys-source-store`, `recsys-event-stream`, `recsys-feature-store`,
`recsys-kafka-connect`, `recsys-streaming`, and `recsys-airflow`.
