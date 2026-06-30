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

Build and push the expected images after `terraform apply` creates Artifact Registry, or create the repository first with a targeted apply:

```bash
cd infra/terraform/gcp
terraform init
terraform apply -target=google_artifact_registry_repository.docker

PROJECT_ID=your-gcp-project-id
REGION=asia-southeast1
REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/recsys"
TAG=gcp

gcloud auth configure-docker "${REGION}-docker.pkg.dev"

docker build -f ../../../infra/docker/Dockerfile.base-python -t recsys-base-python:local ../../..
docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:local -f ../../../apps/data-platform/Dockerfile.dataflow-cli -t "${REPO}/recsys-dataflow-cli:${TAG}" ../../..
docker build -f ../../../apps/data-platform/Dockerfile.spark -t "${REPO}/recsys-spark:${TAG}" ../../..
docker build -f ../../../apps/data-platform/Dockerfile.flink -t "${REPO}/recsys-flink:${TAG}" ../../..
docker build -f ../../../infra/docker/Dockerfile.kafka-connect -t "${REPO}/recsys-kafka-connect:${TAG}" ../../..
docker build -f ../../../infra/docker/Dockerfile.airflow -t "${REPO}/recsys-airflow:${TAG}" ../../..
docker build -f ../../../infra/docker/Dockerfile.mlflow -t "${REPO}/recsys-mlflow:${TAG}" ../../..
docker build -f ../../../apps/api-serving/Dockerfile -t "${REPO}/recsys-api-serving:${TAG}" ../../..
docker build -f ../../../apps/ml-system/Dockerfile.training -t "${REPO}/recsys-mlops-training:${TAG}" ../../..

docker push "${REPO}/recsys-dataflow-cli:${TAG}"
docker push "${REPO}/recsys-spark:${TAG}"
docker push "${REPO}/recsys-flink:${TAG}"
docker push "${REPO}/recsys-kafka-connect:${TAG}"
docker push "${REPO}/recsys-airflow:${TAG}"
docker push "${REPO}/recsys-mlflow:${TAG}"
docker push "${REPO}/recsys-api-serving:${TAG}"
docker push "${REPO}/recsys-mlops-training:${TAG}"
```

## Deploy

```bash
cd infra/terraform/gcp
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars

terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

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

The down command records the live node-pool sizes in `.gcp-services-power-state.env` and the up command restores from that file. Override the defaults only if the cluster was created with different names:

```bash
GCP_PROJECT_ID=fsds-coursework \
GKE_ZONE=asia-southeast1-b \
GKE_CLUSTER=recsys-mlops-gke \
make gcp-services-up
```

## Verify

Static verification from the repo:

```bash
infra/terraform/gcp/scripts/verify_gcp_stack.sh static
```

Live verification after apply:

```bash
cd infra/terraform/gcp
terraform output -raw kubectl_get_credentials_command | bash
./scripts/verify_gcp_stack.sh live
```

The live check confirms GPU node presence, core rollouts, KServe/Triton objects, API service, KEDA scaled objects, and the RayJob.
