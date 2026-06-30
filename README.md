# Full Data & ML system

## Business Domain

This project is an end-to-end e-commerce recommendation system. It simulates product catalog, users, sessions, impressions, behavior events, and orders, then turns those raw events into offline and online features for a BST-style recommender. The platform covers the practical MLOps path: generate and ingest data, process batch/stream features, validate/govern data, train and register models, serve recommendations through FastAPI/Triton, and observe the running system.

## Table Of Contents

- [Submission docs](#submission-docs)
- [Repository structure](#repository-structure)
- [High-level deployment diagrams](#high-level-deployment-diagrams)
- [Data platform](#data-platform)

## Submission docs

Detailed final-coursework proof documents live under `docs/submission/rubic-final-coursework-(final-ml)/`. README stays as a short navigation page; use the docs below for screenshots, commands, observed outputs, and design notes.

Mini-coursework proof documents live under `docs/submission/rubic-(mini-coursework)/`.

| Mini-coursework area | Document |
|---|---|
| Mini proof index | [README.md](docs/submission/rubic-(mini-coursework)/README.md) |
| Docker and Docker Compose | [docker.md](docs/submission/rubic-(mini-coursework)/docker.md) |
| Data generator | [data_generator.md](docs/submission/rubic-(mini-coursework)/data_generator.md) |
| Processing jobs | [processing_jobs.md](docs/submission/rubic-(mini-coursework)/processing_jobs.md) |
| Data storage optimization | [data_storage.md](docs/submission/rubic-(mini-coursework)/data_storage.md) |
| Data pipeline orchestration | [data_pipeline_orchestration.md](docs/submission/rubic-(mini-coursework)/data_pipeline_orchestration.md) |
| Data governance | [data_governance.md](docs/submission/rubic-(mini-coursework)/data_governance.md) |
| Schema design | [schema_design.md](docs/submission/rubic-(mini-coursework)/schema_design.md) |
| Novel ideas | [novel_ideas.md](docs/submission/rubic-(mini-coursework)/novel_ideas.md) |

| Area | Document |
|---|---|
| Infrastructure as Code on GCP/GKE | [iac.md](docs/submission/rubic-final-coursework-(final-ml)/iac.md) |
| Routing, gateway, auth, rate limit | [routing_gateway.md](docs/submission/rubic-final-coursework-(final-ml)/routing_gateway.md) |
| Observability dashboards and telemetry data | [observability.md](docs/submission/rubic-final-coursework-(final-ml)/observability.md) |
| A/B testing for inference services | [ab_testing.md](docs/submission/rubic-final-coursework-(final-ml)/ab_testing.md) |
| Security: centralized secrets and service mesh auth | [security.md](docs/submission/rubic-final-coursework-(final-ml)/security.md) |
| Repository design and design patterns | [repository_design.md](docs/submission/rubic-final-coursework-(final-ml)/repository_design.md) |
| Low-level ML design: 5 key classes | [low_level_ml_design.md](docs/submission/rubic-final-coursework-(final-ml)/low_level_ml_design.md) |

## Repository Structure

```text
apps/
  api-serving/          FastAPI recommendation API and Triton/Redis clients.
  data-platform/        Data generator, CDC ingest, Spark/Flink features, Airflow DAGs, DataHub metadata, drift checks.
  ml-system/            Training code, Kubeflow pipelines, MLflow/model promotion, Triton model packaging.
configs/
  local/                Local and Kubernetes runtime configs for generator, Spark, Flink, Airflow, feature store.
docs/
  pngs/                 Screenshot proof assets for rubric submission.
  submission/           Mini-coursework and final-coursework proof documents.
infra/
  cloudbuild/           GCP Cloud Build image pipeline.
  docker/               Local Docker Compose and base service images.
  helm/                 Kubernetes charts for data platform, serving, observability, gateway, security.
  terraform/gcp/        Terraform-managed GCP/GKE infrastructure and Helm releases.
jenkins/
  scripts/              CI/CD component detection, test, build, deploy, and validation scripts.
tests/
  unit/                 Unit and contract tests for data platform, API serving, ML system.
```

## High-Level Deployment Diagrams

Each major box below is a deployable/runtime unit such as Kafka, Flink, Airflow, Redis, MinIO, Kubeflow, MLflow, KServe/Triton, API Serving, Grafana, DataHub, or NGINX Gateway. Arrows show the main data movement from source events to features, training, serving, and observability.

![Data & ML system](docs/pngs/full_flow.png)

# Data platform

![Data platform](docs/pngs/data_flow.png)

CDC ingestion details live in [apps/data-platform/README.md](apps/data-platform/README.md).
