# End-to-End E-commerce Recommendation Platform

A **production-style, end-to-end recommendation platform** for data engineering, machine learning, deployment, serving, governance, and observability workflows on Kubernetes.

## 🛍️ Business Domain

This project is an end-to-end recommendation platform for e-commerce. It turns catalog, user, session, impression, behavior, and order data into batch and real-time features, trains a Behavior Sequence Transformer (BST), and serves personalized Top-K product recommendations through a production-style MLOps workflow.

---

## 📝 System Overview

- **Data and analytics platform:** Generates configurable historical and real-time e-commerce events in PostgreSQL and MinIO, then streams CDC records through Debezium and Kafka. Spark builds batch features and Iceberg Bronze/Silver/Gold tables, while Flink handles event-time processing, deduplication, watermarking, streaming quality windows, and online feature updates. Airflow orchestrates ingestion, validation, compaction, materialization, drift, and analytics workflows; Feast serves PostgreSQL offline features and Redis online features; Hudi, DataHub, Trino, dbt, Superset, and Evidently provide dataset versioning, lineage, governed analytics, data quality, and drift monitoring.

- **ML training and retraining platform:** Trains a PyTorch Behavior Sequence Transformer with time-aware datasets, negative sampling, ranking metrics, checkpointing, and ONNX/Triton model packaging. Kubeflow Pipelines coordinates data preparation, KubeRay/Ray Tune hyperparameter search and distributed training, evaluation, and promotion. MLflow uses PostgreSQL for tracking and registry metadata and MinIO for artifacts and versioned models; offline NDCG gates, feature-drift checks, and online candidate error/latency gates control promotion and drift-triggered retraining.

- **Serving, infrastructure, and delivery:** FastAPI retrieves Feast online features, calls the Triton V2 inference API, ranks candidates, and returns personalized Top-K recommendations through NGINX. KServe manages stable and candidate Triton deployments, while KEDA HTTP/resource scalers and HPA policies autoscale API and inference workloads. Terraform and Helm provision GCP/GKE and Kubernetes resources; Jenkins and Cloud Build automate testing, image publishing, and deployment with shadow traffic, sticky progressive A/B rollout, model promotion, champion fallback, Helm rollback, and candidate cleanup.

- **Web UI module:** Provides a React, TypeScript, Vite, and TanStack Query storefront served by a non-root NGINX container, backed by a same-origin FastAPI API. The backend uses a bounded PostgreSQL connection pool for transactional user, event, and order writes, and calls the feature and recommendation services to exercise the complete `PostgreSQL → Debezium → Kafka → Flink → Redis/Feast → Triton` real-time path. The frontend and backend are released atomically with Helm and include ingress routing, PDBs, External Secrets, Prometheus/OpenTelemetry instrumentation, CI security checks, deployment smoke tests, and revision-based rollback.

- **Security and observability:** Vault and External Secrets Operator manage runtime credentials; Istio mTLS, authorization policies, and Kubernetes NetworkPolicies secure service-to-service communication. Prometheus and Pushgateway collect infrastructure, pipeline, quality, drift, API, and model-rollout metrics; Grafana provides dashboards and alerts, Loki/Promtail centralize logs, and Tempo/OpenTelemetry provide distributed tracing.

---

## 🎬 Recommendation Web Demo

The demo shows the production web flow from user interactions and streaming feature updates through personalized recommendation serving.

[![Recommendation Web Demo](docs/pngs/web_demo.gif)](docs/pngs/web_demo.mp4)

---

## 📚 Table of Contents

1. [🛍️ Business Domain](#-business-domain)
2. [📝 System Overview](#-system-overview)
3. [🎬 Recommendation Web Demo](#-recommendation-web-demo)
4. [🏗️ Architecture](#-architecture)
   - [Overall System Flow](#overall-system-flow)
   - [Serving Pipeline High-Level Architecture](#serving-pipeline-high-level-architecture)
   - [Progressive A/B Testing and Automatic Rollback](#progressive-ab-testing-and-automatic-rollback)
   - [Data Platform Pipeline](#data-platform-pipeline)
   - [Ranking Sequence Model Architecture](#ranking-sequence-model-architecture)
5. [📁 Repository Main Folder Structure](#-repository-main-folder-structure)
6. [📖 Code Documentation Standards](#-code-documentation-standards)
7. [🗂️ Coursework Documentation](#-coursework-documentation)

---

## 🏗️ Architecture

### Overall System Flow

The following diagram presents the **End-to-End Platform** architecture documented in [high-level system design](<docs/submission/rubic-final-coursework-(final-ml)/high_level_system_design.md>).

![End-to-End Recommendation Platform Architecture](docs/pngs/overview_final.png)

### Serving Pipeline High-Level Architecture

The serving module retrieves fresh online features, routes stable, candidate, or shadow traffic, scores candidates with KServe/Triton, and returns Top-K recommendations.

```mermaid
flowchart LR
  subgraph UX2["User / Client"]
    direction TB
    EndUser2["End User"]
    Client2["Client Application"]
    EndUser2 --> Client2
  end

  subgraph API2["API Serving"]
    direction TB
    Gateway2["NGINX HTTPS Gateway"]
    RecAPI2["Recommendation FastAPI"]
    FeatureAPI2["Online Feature FastAPI"]
    Router2{"Stable / A-B / Shadow Router"}

    Gateway2 --> RecAPI2
    RecAPI2 --> FeatureAPI2 --> RecAPI2
    RecAPI2 --> Router2
  end

  subgraph FS2["Online Feature Store"]
    direction TB
    Redis2[("Feast Online Store / Redis<br/>user, item and candidate features")]
  end

  FeatureAPI2 -->|Feast SDK get_online_features| Redis2
  Redis2 -->|online features| FeatureAPI2

  subgraph MS2["Model Serving"]
    direction TB
    Stable2["Stable KServe + Triton"]
    Candidate2["Candidate KServe + Triton"]
    Scores2["Candidate Scores"]
    TopK2["Top-K Ranking"]
    Response2["Recommendation Response<br/>items + model + experiment metadata"]

    Stable2 --> Scores2
    Candidate2 --> Scores2
    Scores2 --> TopK2 --> Response2
  end

  Router2 -->|control| Stable2
  Router2 -->|candidate| Candidate2
  Router2 -.->|shadow copy| Candidate2

  Client2 -->|recommendation request| Gateway2
  Response2 -->|recommendations| Client2
  Client2 --> EndUser2

  subgraph MD2["Model Delivery"]
    direction TB
    ModelStore2[("Versioned Model Store")]
    ModelCD2["Jenkins Model CD<br/>shadow, A/B, promote, fallback"]
    ModelStore2 --> ModelCD2
  end
  ModelCD2 -.-> Stable2
  ModelCD2 -.-> Candidate2

  subgraph OBS2["Serving Observability"]
    direction TB
    Prometheus2["Prometheus Metrics"]
    Loki2["Promtail + Loki Logs"]
    OTel2["OpenTelemetry OTLP"]
    Tempo2["Tempo Traces"]
    Grafana2["Grafana Dashboards"]

    Prometheus2 --> Grafana2
    Loki2 --> Grafana2
    OTel2 --> Tempo2 --> Grafana2
  end
  RecAPI2 -.-> Prometheus2
  RecAPI2 -.-> Loki2
  RecAPI2 -.-> OTel2
  FeatureAPI2 -.-> Prometheus2
  FeatureAPI2 -.-> Loki2
  FeatureAPI2 -.-> OTel2
  Stable2 -.-> Prometheus2
  Candidate2 -.-> Prometheus2

  classDef edge fill:#fff3e0,stroke:#ef6c00,color:#4e342e;
  classDef service fill:#e3f2fd,stroke:#1976d2,color:#0d47a1;
  classDef store fill:#e8f5e9,stroke:#388e3c,color:#1b5e20;
  classDef model fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c;
  classDef result fill:#fce4ec,stroke:#c2185b,color:#880e4f;

  class EndUser2,Client2,Gateway2 edge;
  class RecAPI2,FeatureAPI2,ModelCD2 service;
  class Redis2,ModelStore2 store;
  class Router2,Stable2,Candidate2 model;
  class Scores2,TopK2,Response2,Prometheus2,Loki2,OTel2,Tempo2,Grafana2 result;
```

### Progressive A/B Testing and Automatic Rollback

The model-delivery controller turns an MLflow candidate into a shadow deployment, then progressively exposes sticky user traffic at **10% → 25% → 50%**. At every step, Jenkins evaluates fresh control/candidate samples from Prometheus; a regression immediately restores champion-only traffic, while a final pass promotes the candidate and removes the temporary Triton service. See [A/B testing](<docs/submission/rubic-final-coursework-(final-ml)/ab_testing.md>) and [progressive rollout details](<docs/submission/rubic-final-coursework-(final-ml)/noval_ideas.md>).

```mermaid
flowchart TD
  Candidate["Kubeflow registers versioned model<br/>MLflow candidate = test"] --> Watcher["Rollout watcher claims candidate<br/>and triggers Jenkins Model CD"]
  Watcher --> Shadow["Deploy candidate KServe/Triton<br/>shadow traffic, user response from champion"]
  Shadow --> ShadowGate{"Candidate Ready and<br/>shadow health gates pass?"}

  ShadowGate -->|No| Rollback["Automatic rollback<br/>A/B weight = 0, shadow disabled"]
  ShadowGate -->|Yes| AB10["Sticky A/B rollout<br/>candidate weight = 10%"]

  AB10 --> Samples["Collect fresh control/candidate samples<br/>Prometheus + Grafana"]
  AB25["Increase candidate weight to 25%"] --> Samples
  AB50["Increase candidate weight to 50%"] --> Samples
  Samples --> SampleGate{"At least 100 fresh samples<br/>for both variants?"}
  SampleGate -->|No: HOLD| Samples
  SampleGate -->|Yes| OnlineGate{"Online gates pass?<br/>error delta <= 0.02<br/>p95 latency <= 1.5x control<br/>confidence >= 0.95x control"}

  OnlineGate -->|Fail| Rollback
  OnlineGate -->|Pass at 10%| AB25
  OnlineGate -->|Pass at 25%| AB50
  OnlineGate -->|Pass at 50%| Promote["Promote candidate<br/>update stable manifest and MLflow champion"]

  Rollback --> Stable["Delete candidate Triton<br/>serve previous champion only"]
  Promote --> NewStable["Delete temporary candidate Triton<br/>serve new champion only"]

  classDef control fill:#e3f2fd,stroke:#1976d2,color:#0d47a1;
  classDef candidate fill:#fff3e0,stroke:#ef6c00,color:#4e342e;
  classDef decision fill:#fff8e1,stroke:#f9a825,color:#5d4037;
  classDef safe fill:#e8f5e9,stroke:#388e3c,color:#1b5e20;
  classDef rollback fill:#ffebee,stroke:#c62828,color:#b71c1c;

  class Candidate,Watcher control;
  class Shadow,AB10,AB25,AB50,Samples candidate;
  class ShadowGate,SampleGate,OnlineGate decision;
  class Promote,Stable,NewStable safe;
  class Rollback rollback;
```

### Data Platform Pipeline

The data platform combines batch and CDC ingestion, Spark and Flink processing, Airflow orchestration, data-quality checks, DataHub lineage, and Feast offline/online feature stores.

```mermaid
flowchart LR
  subgraph Sources["Source Simulation"]
    Generator["Historical + Realtime<br/>Data Generator"]
    SourceDB[("Operational PostgreSQL")]
    Raw[("Raw Data / MinIO")]
    Generator -->|historical files| Raw
    Generator -->|realtime events| SourceDB
  end

  subgraph Processing["Ingestion & Processing"]
    Debezium["Debezium CDC"]
    Kafka["Kafka"]
    BronzeLake[("Iceberg Bronze Tables")]
    Spark["Spark Batch<br/>Feature Engineering"]
    SilverGoldLake[("Iceberg Silver / Gold Tables")]
    Flink["Flink Realtime<br/>Feature Engineering"]

    Raw --> BronzeLake --> Spark --> SilverGoldLake
    SourceDB --> Debezium --> Kafka --> Flink
  end

  subgraph Features["Feast Feature Store"]
    Offline[("Offline Store<br/>PostgreSQL")]
    Materialize["Feast Materialization"]
    Online[("Online Store<br/>Redis")]

    Offline --> Materialize --> Online
  end

  Spark -->|batch features| Offline
  Flink -->|stream features| Offline
  Flink -->|fresh online features| Online

  subgraph Control["Orchestration, Quality & Governance"]
    Airflow["Airflow Orchestration"]
    DataChecks["Data Quality + Pipeline Health"]
    DataHub["DataHub Catalog + Lineage"]
  end

  Airflow -.-> Spark
  Airflow -.-> Flink
  Airflow -.-> Materialize
  Spark -.-> DataChecks
  Flink -.-> DataChecks
  Kafka -.-> DataChecks
  BronzeLake -.-> DataHub
  SilverGoldLake -.-> DataHub
  Kafka -.-> DataHub
```

### Ranking Sequence Model Architecture

The ranking model follows the architecture in [*Behavior Sequence Transformer for E-commerce Recommendation in Alibaba*](https://arxiv.org/pdf/1905.06874): positional and item features represent the ordered behavior sequence, a Transformer captures dependencies between interactions, and its target-item representation is combined with user, item, context, and cross features for CTR prediction.

```mermaid
flowchart LR
  subgraph Inputs["Input Features"]
    Other["Other Features<br/>user, item, context, cross"]
    SequenceItems["Behavior Sequence + Target Item<br/>item_id, category_id"]
    Position["Positional Features<br/>relative interaction time"]
  end

  Other --> OtherEmbedding["Other Feature Embeddings"]
  SequenceItems --> SequenceEmbedding["Sequence Item Embeddings"]
  Position --> SequenceEmbedding
  SequenceEmbedding --> Transformer["Transformer Block<br/>Multi-Head Self-Attention + FFN"]
  Transformer --> TargetRepresentation["Target-Item Sequence Representation"]
  OtherEmbedding --> Concatenate["Concatenate"]
  TargetRepresentation --> Concatenate
  Concatenate --> MLP["Three-Layer MLP"]
  MLP --> Sigmoid["Sigmoid"]
  Sigmoid --> CTR["Click-Through Probability / Ranking Score"]
```

---

## 📁 Repository Main Folder Structure

```txt
├── apps/                         # Deployable product and data/ML workloads
│   ├── analytics/                # Analytics models and dashboard bootstrap
│   ├── api-serving/              # Online feature and recommendation APIs
│   ├── data-platform/            # Ingestion, processing, orchestration, feature store, and governance
│   └── ml-system/                # Training, experimentation, model promotion, and serving packaging
├── configs/                      # Versioned environment and service configuration
├── docs/                         # Architecture, design, and coursework documentation
├── infra/                        # Local and cloud infrastructure definitions
│   ├── cloudbuild/               # Cloud image build pipelines
│   ├── docker/                   # Docker images and local Compose runtime
│   ├── helm/                     # Kubernetes application charts
│   ├── k8s/                      # Kubernetes manifests and cluster lifecycle scripts
│   ├── kubeflow/                 # Kubeflow pipeline deployment artifacts
│   └── terraform/                # Cloud infrastructure as code
├── jenkins/                      # CI/CD jobs, model rollout, and deployment automation
├── notebooks/                    # Tracked exploration and ML workflow notebooks
└── tests/                        # Unit, contract, integration, end-to-end, and load tests
```

---

## 🗂️ Coursework Documentation

The two tables below convert the major sections from the first two tabs of [Coursework Tracking (Public).xlsx](<docs/xlsx/Coursework Tracking (Public).xlsx>) into navigable documentation indexes.

### Data Platform

Source: tab **`rubic (mini-coursework)`**.

| Rubric area | Coverage |
| --- | --- |
| [README and high-level design](README.md) | Business domain, repository structure, table of contents, and deployable-unit architecture. |
| [Engineering Fundamentals](<docs/submission/rubic-(mini-coursework)/docker.md>) | Docker, Docker Compose, multi-stage builds, and image-size optimization. |
| [Implement Data Generator](<docs/submission/rubic-(mini-coursework)/data_generator.md>) | Offline skew, high cardinality, schema evolution, duplicates, streaming burst/late events, configuration, and raw storage. |
| [Processing Jobs](<docs/submission/rubic-(mini-coursework)/processing_jobs.md>) | Spark offline processing, Flink streaming processing, optimization evidence, pipeline integration, and window processing. |
| [Data Storage](<docs/submission/rubic-(mini-coursework)/data_storage.md>) | Lakehouse compaction/partitioning and data-warehouse indexing. |
| [Data Pipeline Orchestration](<docs/submission/rubic-(mini-coursework)/data_pipeline_orchestration.md>) | Airflow DP1, DP2, and DP3 ingest/validate stages. |
| [Data Governance](<docs/submission/rubic-(mini-coursework)/data_governance.md>) | DataHub lineage, validation, and data contracts for DP1, DP2, and DP3. |
| [Schema Design](<docs/submission/rubic-(mini-coursework)/schema_design.md>) | Zone schemas, SCD2 dimensions, feature timestamps, table relationships, and naming conventions. |
| [Novel Ideas](<docs/submission/rubic-(mini-coursework)/novel_ideas.md>) | Grafana-based data-quality monitoring and analytics-platform extensions. |

### ML System

Source: tab **`rubic final-coursework (final -`**.

| Rubric area | Coverage |
| --- | --- |
| [High-Level System Design](<docs/submission/rubic-final-coursework-(final-ml)/high_level_system_design.md>) | End-to-end deployment, serving, model, infrastructure, security, and delivery architecture. |
| [Web API: Pull Online Features](<docs/submission/rubic-final-coursework-(final-ml)/web-api-pull-data.md>) | FastAPI, Pydantic validation, async feature retrieval, health checks, Helm rollout, and fallback. |
| [Web API: Model Prediction](<docs/submission/rubic-final-coursework-(final-ml)/web-api-model-prediction.md>) | Online features, Triton request construction, inference, ranking, and response validation. |
| [Real-Time Drift Detection and ML Telemetry](<docs/submission/rubic-final-coursework-(final-ml)/observability.md>) | Drift telemetry, scheduled comparison, dashboards, and Kubeflow retraining trigger. |
| [Autoscale](<docs/submission/rubic-final-coursework-(final-ml)/autoscale.md>) | KEDA/HPA autoscaling for APIs and Triton with load-test evidence. |
| [Validation & Verification](<docs/submission/rubic-final-coursework-(final-ml)/validation_verification.md>) | Coverage, fixtures/mocks, equivalence partitions, boundary values, mutation/property-based tests, and load tests. |
| [Improve the Data Generator](<docs/submission/rubic-final-coursework-(final-ml)/improve_data_generator.md>) | Configurable data drift and ID-label generation for training joins. |
| [Feature Store](<docs/submission/rubic-final-coursework-(final-ml)/feature_store.md>) | Incremental materialization, streaming writes to offline/online stores, and TTL design. |
| [ML](<docs/submission/rubic-final-coursework-(final-ml)/ml.md>) | Feast training-data retrieval, train/validation split, BST training, evaluation, and model saving. |
| [ML Pipelines](<docs/submission/rubic-final-coursework-(final-ml)/ml_pipelines.md>) | Kubeflow pipeline stages, Ray Tune, distributed training, evaluation, and promotion. |
| [Versioning](<docs/submission/rubic-final-coursework-(final-ml)/versioning.md>) | MLflow model versioning and incremental data versioning. |
| [CI/CD](<docs/submission/rubic-final-coursework-(final-ml)/ci_cd.md>) | CI/CD for materialization, training, DP1–DP3, APIs, inference, drift detection, and streaming jobs. |
| [Routing & Gateway](<docs/submission/rubic-final-coursework-(final-ml)/routing_gateway.md>) | NGINX gateway, hidden services, authentication, rate limits, domains, and HTTPS. |
| [Infrastructure as Code](<docs/submission/rubic-final-coursework-(final-ml)/iac.md>) | Terraform-managed GCP/GKE services and infrastructure layout. |
| [Observability](<docs/submission/rubic-final-coursework-(final-ml)/observability.md>) | API and infrastructure metrics, logs, traces, Grafana dashboards, and drift monitoring. |
| [A/B Testing](<docs/submission/rubic-final-coursework-(final-ml)/ab_testing.md>) | Stable/candidate traffic split and per-version monitoring. |
| [Security](<docs/submission/rubic-final-coursework-(final-ml)/security.md>) | Centralized secret management, service-mesh authentication, mTLS, and authorization. |
| [Repository Design](<docs/submission/rubic-final-coursework-(final-ml)/repository_design.md>) | Clean repository boundaries, clean code, and design-pattern evidence. |
| [Low-Level ML Design](<docs/submission/rubic-final-coursework-(final-ml)/low_level_ml_design.md>) | Five key service classes and their implementation mappings. |
| [Novel Ideas](<docs/submission/rubic-final-coursework-(final-ml)/noval_ideas.md>) | Automated shadow deployment, progressive A/B gates, promotion, fallback, and cleanup. |
