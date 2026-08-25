# GCP Full-Stack Deployment Checklist

Production target: `recsys-mlops-506406`, `asia-southeast1-b`, `recsys-mlops.site`.

Run `make gcp-full-check GCP_CHECK_MODE=preflight` before provisioning and
`make gcp-full-check GCP_CHECK_MODE=all` after deployment. The command writes
machine-readable results to `reports/gcp/full-stack-check.json`.

Run Terraform mutations through `ops/gcp/terraform_gcp.sh`. The wrapper refuses
the wrong gcloud account/project and selects the matching refreshable legacy
credential instead of the stale machine ADC. It also uses a project-specific
`TF_DATA_DIR`, so historical local workspaces are never migrated into this
project's state bucket.

## GCP foundation

- [ ] Billing linked; guarded gcloud project and active account verified.
- [ ] Artifact Registry, Cloud KMS, Resource Manager, Compute, GKE, IAM,
  Logging, Monitoring, Storage, and Service Usage APIs enabled.
- [ ] Versioned `gs://recsys-mlops-506406-tfstate` backend initialized.
- [ ] `recsys` Artifact Registry and lake/model backup buckets exist.
- [ ] VPC, subnet, secondary Pod/Service ranges, GKE control plane, Workload
  Identity, IAM and service accounts exist.
- [ ] The cost profile has exactly two non-Spot `e2-standard-8` CPU nodes and
  one non-Spot `e2-standard-4` ML node. Dedicated LLM and GPU pools are absent;
  `ml_compute_mode=cpu` is selected.

## Kubernetes platform and releases

- [ ] Controllers: cert-manager, KEDA, KEDA HTTP add-on, External Secrets,
  Istio, ingress-nginx, Kubeflow Pipelines, KServe, KubeRay, Prometheus operator.
- [ ] Data: config, MinIO lakehouse, source Postgres, Kafka, Kafka Connect,
  feature Postgres/Redis, Flink streaming and Airflow.
- [ ] ML/serving: MLflow/Postgres/MinIO, runtime PVC/Secret, Ray, KServe/Triton,
  online-feature API, inference API and progressive rollout watcher.
- [ ] RAG/analytics/demo: Milvus, RAG API/index, Trino, dbt, Superset, demo API
  and web frontend.
- [ ] Security/full optional: Vault HA/KMS unseal, DataHub, Substrate, kagent,
  llm-d, Agent Gateway, Agent Registry, feature-RAG MCP/context agent and
  recommendation MCP/agent.
- [ ] Both Qwen replicas are Ready on different `recsys-mlops-cpu` nodes.
- [ ] Observability/CI: Prometheus, Grafana, Loki, Tempo, Promtail, Pushgateway,
  exporters and Jenkins.
- [ ] All Deployments, StatefulSets and DaemonSets ready; all PVCs Bound and
  all ExternalSecrets Ready. Historical terminal batch Pods do not count as
  long-running service failures.

## Delivery inventory

- [ ] All 19 catalog images are present by immutable digest.
- [ ] All 21 product components and `ci_config` pass their gates.
- [ ] All 31 current release-plan deploy units complete in dependency order.
- [ ] Kubeflow BST package is uploaded/versioned and Jenkins uses only the new
  project, cluster context and Artifact Registry.

## Data and model workflows

- [ ] Airflow DP1, DP2, DP3 and Feast materialization runs succeed in order.
- [ ] `make gcp-train-model` runs the existing feature-drift Airflow DAG, exercises its retrain trigger, and waits for the resulting KFP workflow.
- [ ] Kafka/Flink realtime jobs run; Iceberg/Hudi/Postgres/Redis validation and
  governance reports pass.
- [ ] DataHub catalog sync succeeds after datasets exist.
- [ ] RAG item-index run is promoted and golden retrieval checks pass.
- [ ] BST KFP run completes prepare, Ray tune/train, evaluate, Hudi savepoint,
  MLflow registration/promotion and KServe CD.
- [ ] MLflow contains the new model version; stable Triton and recommendation
  Top-K endpoints use the promoted model.

## DNS, TLS, access and final convergence

- [ ] `recsys-mlops.site`, `api`, `metrics`, `logs`, and `traces` A records all
  point to the ingress-nginx LoadBalancer IP.
- [ ] Let’s Encrypt certificates are Ready; unauthenticated protected requests
  are rejected and authenticated HTTPS checks succeed.
- [ ] Final Terraform detailed plan returns exit code 0.
- [ ] Full checklist JSON contains no FAIL entries.
