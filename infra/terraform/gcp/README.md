# GCP Terraform Deployment

This stack provisions a GKE Standard cluster and deploys the RecSys data and ML services with the existing Helm charts:

- Data platform: Postgres, Kafka, Kafka Connect, Redis, MinIO, Flink, Airflow, Spark jobs.
- ML platform: Kubeflow Pipelines, MLflow, runtime PVC/secret, KubeRay, GPU Ray training job.
- Serving: KServe InferenceService backed by Triton on GPU, FastAPI gateway service, KEDA autoscaling.
- Observability: Prometheus, Grafana, Loki, Tempo, Pushgateway.

## Cost And Latency Defaults

The defaults are tuned for moderate cost while keeping inference warm:

- `asia-southeast1` / `asia-southeast1-b` for Vietnam/Singapore traffic.
- CPU pool: `e2-standard-4`, min 2, max 5, non-Spot for stateful services.
- GPU pool: `n1-standard-8` + 1 `nvidia-tesla-t4`, min 1, max 2.
- Triton requests 1 GPU and KEDA caps Triton at 2 replicas.
- Ray training uses 1 GPU per trial and one worker by default.

Set `gpu_min_nodes = 0` for dev cost saving, or `gpu_spot = true` for interruptible training-only environments. Keep `gpu_spot = false` when Triton must stay warm.

## Prerequisites

1. `gcloud`, `kubectl`, `helm`, and `terraform` installed.
2. GCP project with billing enabled.
3. GPU quota in `var.zone` for `var.gpu_accelerator_type`.
4. Docker images pushed to Artifact Registry before app workloads roll out.

Create Artifact Registry before the first Jenkins image publication:

```bash
cd infra/terraform/gcp
terraform init
terraform apply -target=google_artifact_registry_repository.docker
```

Terraform bootstraps the application Helm releases once. Their
`lifecycle.ignore_changes = all` handoff makes Jenkins the only runtime release
operator, so a later infrastructure apply cannot roll image digests back.
Terraform remains the owner of namespaces, operators, the central secret
payloads and the `recsys-security` ExternalSecret release.

Jenkins validates `images/catalog.json`, builds the 15-image dependency graph,
pushes immutable references, and deploys only catalog-owned artifacts.

## One-time hard-cut migration

The old `recsys-data-platform` Helm release cannot be upgraded in place into
multiple release owners. Before the first apply of this refactor, schedule a
maintenance window and:

1. Back up the Terraform state, the old Helm values/manifest, and all
   production databases/object storage.
2. Back up `recsys-data-platform-secret` to a mode-`0600` file outside the
   repository. The old release may delete its copy during uninstall; the
   ExternalSecret must recreate it before workloads resume.
3. Remove only the retired Terraform state address:
   `terraform state rm helm_release.recsys_data_platform`.
4. Uninstall the old release:
   `helm uninstall recsys-data-platform -n recsys-dataflow`.
   StatefulSet volume-claim templates retain their PVCs, but workloads are
   unavailable until the next step finishes.
5. Run `terraform plan` and verify that it creates the eight
   `recsys-data-*`/`recsys-airflow` releases without replacing persistent
   disks or PVCs, then apply the reviewed plan.
6. Wait for `ExternalSecret/recsys-data-platform-secret` to become Ready and
   confirm the target Secret exists. Securely delete the temporary backup only
   after the live validation succeeds.
7. Run `ops/validation/verify_gcp_stack.sh live` before Jenkins deployment is
   re-enabled.

The split application releases deliberately stay in Terraform state as
bootstrap records, but Terraform ignores their runtime mutations. Do not remove
the split release state addresses and do not run `terraform import` after
Jenkins upgrades them.

Cloud Build retirement is also a two-apply operation against the pre-refactor
state: first change only the Cloud Build API instance to
`disable_on_destroy = true` and apply; then use this final configuration, which
removes the API entry and Cloud Build IAM resources, and apply again. Do not
skip the reviewed intermediate apply if the API must be disabled rather than
merely unmanaged.

## Deploy

```bash
cd infra/terraform/gcp
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars

terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

After apply, prove that Terraform will not revert Jenkins-owned runtime values:

```bash
terraform plan -detailed-exitcode
```

Review any exit code `2`; application image-only drift must not appear.

## Hibernate And Resume Without Deleting PVC Data

Use these commands when you want to stop paying for GKE worker nodes while keeping PVC/PV-backed data such as MinIO, Postgres, Airflow, MLflow, and DataHub volumes.

```bash
# Bring all RecSys GCP services down by scaling node pools to 0.
# This keeps namespaces, Helm releases, PVCs, PVs, and Persistent Disks.
make gcp-services-down

# Bring node pools back, wait rollouts, and run smoke checks.
make gcp-services-up

# Inspect node pools, PVCs, nodes, and non-running pods.
make gcp-services-status
```

The down command records the live node-pool sizes in `.gcp-services-power-state.env` and snapshots every PVC name, UID, and PV binding in `.gcp-services-power-state.env.pvcs`. The up command restores the node pools, verifies that the PVC identities and bindings are unchanged, waits for Deployments, StatefulSets, and DaemonSets in every namespace, and then runs service smoke checks. A failed or interrupted down keeps the original pre-hibernate snapshot, so rerunning the command is safe. Override the defaults only if the cluster was created with different names:

```bash
GCP_PROJECT_ID=recsys-mlops \
GKE_ZONE=asia-southeast1-b \
GKE_CLUSTER=recsys-mlops-gke \
make gcp-services-up
```

For the coursework-sized GKE cluster, `make gcp-services-up` also normalizes runtime settings so the full data and ML platform comes back in the same proof-ready shape:

- KEDA HTTP add-on `external-scaler` and `interceptor` default to `1` replica each. Its three control-plane deployments use the coursework request profile (`25m` CPU and `20Mi` memory each). The services stay enabled, but this leaves enough schedulable headroom for Airflow, KFP component pods and the Ray retrain launcher on the fixed two-node cluster.
- Istiod uses the proof-cluster request profile (`50m` CPU and `256Mi` memory, with `500m`/`1Gi` limits) instead of reserving the chart default `500m` CPU and `2Gi` memory. This keeps STRICT mTLS and authorization enforcement enabled while retaining `500m` schedulable headroom for a KFP/Ray launcher.
- Airflow data-platform config is restored to `REALTIME_E2E_ENABLED=true` and `RETRAIN_PSI_THRESHOLD=0.15`, so a forced-drift proof run does not leave the cluster in forced mode.
- The smoke phase checks the recommendation API, Flink streaming job, Jenkins UI, Airflow UI, DataHub UI/GMS, Prometheus, Grafana, and a temporary `500m` CPU Ray-launcher scheduling pod. A/B split is checked when A/B is enabled; set `GCP_SERVICES_REQUIRE_AB_TEST=1` to require an active candidate deployment.
- Service readiness covers controller-owned pods and every Deployment, StatefulSet, and DaemonSet. Retained Airflow/KFP/Argo batch pods from historical runs remain visible in `make gcp-services-status`, but their terminal `Error` or `Pending` state does not falsely mark the long-running services as unavailable.
- Smoke port-forwards use non-default local ports to avoid clashing with proof UIs already open locally: Jenkins `28090`, Airflow `28080`, DataHub GMS `28088`, DataHub frontend `29002`, Prometheus `29090`, and Grafana `23000`.

If you specifically need KEDA HTTP add-on HA for an autoscaling demo, override the replica count:

```bash
GCP_SERVICES_KEDA_HTTP_REPLICAS=3 make gcp-services-up
```

## Verify

Static verification from the repo:

```bash
ops/validation/verify_gcp_stack.sh static
```

Live verification after apply:

```bash
terraform output -raw kubectl_get_credentials_command | bash
ops/validation/verify_gcp_stack.sh live
```

The live check confirms GPU node presence, core rollouts, KServe/Triton objects, API service, KEDA scaled objects, and the RayJob.
