# Repository Design Proof

This proof covers the final-coursework rubric item **Repository Design: clean code, clean repo, and design pattern usage**.

## Folder Layout

The repository is split by service boundary rather than by one large application folder.

```text
apps/
  api-serving/                 FastAPI recommendation API and Triton client
  data-platform/               Airflow, Spark, Flink, Feast, ingestion, validation
  ml-system/                   BST model, Kubeflow/Ray training, model registry/promotion
infra/
  cloudbuild/                  Cloud Build image build configs
  helm/
    recsys-serving/            API + KServe/Triton serving chart
    recsys-data-platform/      Data services and orchestration chart
    recsys-observability/      Prometheus, Grafana, Loki, Tempo, dashboards
    recsys-security/           External Secrets + Istio mTLS/auth policies
    recsys-gateway/            NGINX ingress, auth, rate limit
  terraform/gcp/               GCP/GKE IaC split by infrastructure concern
docs/
  submission/                  Rubric proof documents
tests/
  unit/ contract/ e2e/ load/   Focused tests by confidence level
```

The GCP Terraform layout follows the same service split:

| Terraform file | Responsibility |
|---|---|
| `apis.tf` | Required GCP APIs. |
| `network.tf` | VPC, subnet, IP ranges. |
| `gke.tf` | GKE cluster and node pools. |
| `registry_storage.tf` | Artifact Registry and storage buckets. |
| `cloudbuild.tf` | Cloud Build permissions/config. |
| `namespaces.tf` | Kubernetes namespaces and mesh injection labels. |
| `dependencies.tf` | Shared platform operators: cert-manager, KEDA, KServe, Istio, External Secrets. |
| `recsys_services.tf` | RecSys Helm releases. |
| `secret_management.tf` | Central source secrets for External Secrets Operator. |

## Clean Repo Evidence

Run:

```bash
find apps infra/helm infra/terraform/gcp docs/submission -maxdepth 2 -type d | sort | sed -n '1,120p'
```

### Image proof 

![Ingress LoadBalancer proof](../../pngs/clean_repo_evidence.png)

The important proof is that each large concern has its own deployable boundary:

| Boundary | Owns |
|---|---|
| API serving | Request schema, online feature lookup, Triton route selection, API metrics/traces. |
| Data platform | CDC, batch feature materialization, online feature materialization, Airflow orchestration. |
| ML system | Model architecture, training loop, evaluation metrics, model export/promotion. |
| Observability | Metrics/logs/traces collection and dashboards. |
| Gateway | North-south routing, basic auth, and rate limiting. |
| Security | Secret sync and service-to-service authorization. |

## Design Patterns In Code

| Pattern | Code | Why it matters |
|---|---|---|
| Strategy / Router | `TritonABRouter` chooses control or candidate route at runtime. | A/B routing is isolated from ranking logic. |
| Adapter / Gateway | `FeatureClient` wraps Redis feature-store access. | API code depends on feature operations, not Redis commands everywhere. |
| Template Method style training loop | `Trainer.train`, `Trainer.evaluate`, `_compute_metrics`, `_forward_batch`. | Training/evaluation share batch movement, model forward, and metric computation structure. |
| Composite neural module | `BST` composes embeddings, transformer layer, positional encoding, and MLP. | Model parts are testable and reusable as PyTorch modules. |
| Builder / Manifest generator | `promote_best_model`, `build_triton_repository`, `build_manifest`. | Promotion assembles ONNX, Triton config, storage upload, MLflow registry, and manifest consistently. |

## Tests Showing Boundaries

Useful test groups:

```bash
uv run pytest tests/unit/api_serving tests/contract/test_serving_contracts.py
uv run pytest tests/unit/ml_system
uv run pytest tests/e2e/test_live_serving_flow.py
```

What to capture:

```text
docs/pngs/repository_tests_pass.png
```

## README Policy

`README.md` should remain a summary and navigation page. Detailed proof documents live in `docs/submission/rubic-final-coursework-(final-ml)/`, including:

- `iac.md`
- `routing_gateway.md`
- `observability.md`
- `ab_testing.md`
- `security.md`
- `repository_design.md`
- `low_level_ml_design.md`

