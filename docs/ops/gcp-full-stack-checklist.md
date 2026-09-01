# GCP Full-Stack Deployment Checklist

Production target: `recsys-mlops-506406`, `asia-southeast1-b`, `recsys-mlops.site`.

Current repository target (updated 2026-08-27): GKE
`1.35.7-gke.1027000`, custom kagent build `e6df917`, and Substrate `0.0.11`.
The PodCertificate and ClusterTrustBundle beta APIs are enabled and cannot be
disabled. Production validation must remain paused while Valkey quorum or ATE
API health is red; certificate projection and metric availability alone are
not enough to authorize traffic or Registry cutover.

Run `make gcp-full-check GCP_CHECK_MODE=preflight` before provisioning and
`make gcp-full-check GCP_CHECK_MODE=all` after deployment. The command writes
machine-readable results to `reports/gcp/full-stack-check.json`.

Run Terraform mutations through `ops/gcp/terraform_gcp.sh`. The wrapper refuses
the wrong gcloud account/project and requires the expected identity through
`GCP_ACCOUNT`. It uses standard Application Default Credentials unless
`GCP_TERRAFORM_CREDENTIAL_FILE` is explicitly supplied, and selects a
project-specific `TF_DATA_DIR` so historical local workspaces are never
migrated into another project's state bucket. Authenticate and run it with:

```bash
gcloud auth login <gcp-account>
gcloud auth application-default login
gcloud auth application-default set-quota-project recsys-mlops-506406
gcloud config set project recsys-mlops-506406

GCP_ACCOUNT=<gcp-account> \
  ops/gcp/terraform_gcp.sh -chdir=infra/terraform/gcp plan
```

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
- [ ] After any node-pool recreation, verify Zookeeper did not load an empty
  snapshot: Kafka `/cluster/id` and every persisted `partition.metadata`
  `topic_id` must match Zookeeper before restarting Connect/Flink. Do not delete
  Kafka log directories to resolve an ID mismatch. The production source
  Postgres PVC was expanded online to `30Gi` on 2026-08-26; keep at least 20%
  free before enabling the realtime producer.
- [ ] ML/serving: MLflow/Postgres/MinIO, runtime PVC/Secret, Ray, KServe/Triton,
  online-feature API, inference API and progressive rollout watcher.
- [ ] RAG/analytics/demo: Milvus, RAG API/index, Trino, dbt, Superset, demo API
  and web frontend.
- [ ] Security/full optional: Vault HA/KMS unseal, DataHub, Substrate, kagent,
  llm-d, Agent Gateway, Agent Registry, feature-RAG MCP/context agent and
  recommendation MCP/agent.
- [ ] Context, Recommendation, and Coordinator are `SandboxAgent`s with the
  dedicated WorkerPools `recsys-context-sandbox-pool`,
  `recsys-recommendation-sandbox-pool`, and
  `recsys-coordinator-sandbox-pool`; the regular Coordinator Agent is absent.
- [ ] All three production values select `metricMode: assignedWorkers` and the
  exact `ate_workerpool_workers{ate_worker_state="assigned"}` query with
  threshold `0.7`, range `1..3`, 300-second scale-down, and fallback `1`.
- [ ] Substrate control-plane/ATE and WorkerPool images are `0.0.11`, use the
  native `.status.selector` `/scale` contract, and no `scaleSelector` or `0.0.6`
  HPA post-renderer remains. Valkey stays pinned to `9.1`. Verify
  every Valkey `nodes.conf` `myself` address equals the current Pod IP; the GKE
  post-renderer must inject `POD_IP` and `--cluster-announce-ip`.
- [ ] Before publishing `recsys/recsys-coordinator-agent-sandbox` or retiring
  the regular registry artifact, require context-only, recommendation-only,
  composite, direct-MCP, and partial-failure coordinator gates to pass. The
  production suite covers six cases on the v7 kagent compatibility image.
  Coordinator v22 must compile both specialist tools with
  `isolate_sessions=true`; Recommendation v9 must copy `user_id`, candidates,
  and `top_k` exactly and never continue to `ask_user` after the MCP response.
  The earlier regular-Coordinator routing failure is retained only as
  superseded history in Validation & Verification.
- [ ] Do not run the three autoscale load tests while Valkey reports
  `cluster_state:fail` or ATE API is CrashLooping. Recover quorum first, then
  test Context, Recommendation, and Coordinator sequentially and attach the
  metric, WorkerPool, HPA, ScaledObject, pod, fallback, and routing evidence.
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
- [ ] Do not apply an otherwise mixed Terraform plan merely to converge the
  agent stack; review and isolate unrelated resources such as Cloud Logging.
- [ ] Full checklist JSON contains no FAIL entries.
