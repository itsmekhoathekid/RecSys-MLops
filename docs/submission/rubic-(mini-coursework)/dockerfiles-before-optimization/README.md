# Dockerfiles Before Optimization

This folder stores historical Dockerfile baselines used for the Docker optimization before/after proof.

These files are intentionally not used by the active CI/CD pipeline. They are snapshots from git history so the optimized Dockerfiles in the main repo can be compared against a reproducible pre-optimization baseline.

| Baseline file | Original path | Source commit |
|---|---|---|
| `Dockerfile.base-python.before` | `infra/docker/Dockerfile.base-python` | `4683a24` |
| `Dockerfile.dataflow-cli.before` | `apps/data-platform/Dockerfile.dataflow-cli` | `4683a24` |
| `Dockerfile.data-generator.before` | `apps/data-platform/data-generator/Dockerfile` | `4683a24` |
| `Dockerfile.airflow.before` | `infra/docker/Dockerfile.airflow` | `4683a24` |
| `Dockerfile.spark.before` | `apps/data-platform/Dockerfile.spark` | `c71b9a4` |
| `Dockerfile.flink.before` | `apps/data-platform/Dockerfile.flink` | `c71b9a4` |
| `Dockerfile.api-serving.before` | `apps/api-serving/Dockerfile` | `39450cb` |
| `Dockerfile.mlops-training.before` | `apps/ml-system/Dockerfile.training` | `4683a24` |
| `Dockerfile.mlops-spark.before` | `apps/ml-system/Dockerfile.spark` | `089a835` |
| `Dockerfile.kafka-connect.before` | `infra/docker/Dockerfile.kafka-connect` | `4683a24` |
| `Dockerfile.mlflow.before` | `infra/docker/Dockerfile.mlflow` | `089a835` |
