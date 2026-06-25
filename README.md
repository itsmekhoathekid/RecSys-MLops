# Full Data & ML system

## Local full-service cluster

Use these two commands for the whole local Kubernetes stack.

```bash
make cluster-up
```

`cluster-up` starts the `recsys-mlops` Minikube profile, installs or upgrades the full RecSys service stack, waits for rollouts, and verifies required deployments/services. The default profile resources are 8 CPUs, 16GiB memory, and 40GiB disk. If the cluster needs more memory:

```bash
MINIKUBE_MEMORY_MB=18432 make cluster-up
```

For a fresh Minikube Docker daemon, rebuild local images before installing services:

```bash
RECSYS_CLUSTER_BUILD_IMAGES=1 make cluster-up
```

The full-service stack includes Kubeflow Pipelines, KubeRay, MLflow, MinIO, Postgres, the data platform, KEDA, KServe/Triton serving, FastAPI serving, observability, and the gateway. DataHub is optional because it is a heavier governance add-on:

```bash
RECSYS_CLUSTER_INSTALL_DATAHUB=1 make cluster-up
```

For local macOS/arm64 stability, `cluster-up` scales optional KFP `metadata-writer` and `proxy-agent` deployments to 0 by default. To keep them enabled:

```bash
RECSYS_CLUSTER_SCALE_OPTIONAL_KFP=0 make cluster-up
```

To stop the stack while keeping data, PVCs, MLflow artifacts, MinIO buckets, and model weights:

```bash
make cluster-down
```

`cluster-down` is non-destructive: it prints the retained PVCs/namespaces and stops the Minikube profile. Use it when you want to pause local services and resume later with the same data.

To run the full data setup and verify that the feature store bucket plus Redis online store are populated:

```bash
make cluster-data-setup
```

This starts the full-service stack if needed, triggers `k8s_data_platform_dag`, waits for the Airflow run to finish, then verifies MinIO lake data, feature-store offline paths, warehouse tables, and Redis online feature keys.

To run the full ML path from Kubeflow to serving validation:

```bash
make cluster-mlops-serving-e2e
```

This submits the compiled BST Kubeflow pipeline, waits through MLflow/Ray/evaluation/promotion-manifest creation, runs model CD to Triton/KServe, sends real FastAPI recommendation traffic, and verifies Grafana plus Prometheus serving metrics.

To clean up the full-service stack and delete local Kubernetes data:

```bash
make cluster-destroy
```

`cluster-destroy` uninstalls the RecSys Helm releases, deletes full-service namespaces/PVCs, verifies they are gone, then stops Minikube. To delete the Minikube profile entirely:

```bash
RECSYS_CLUSTER_DELETE_PROFILE=1 make cluster-destroy
```

## I coded whatever you see in this diagram

![Data & ML system](docs/pngs/full_flow.png)

# Data platform

![Data platform](docs/pngs/data_flow.png)

CDC ingestion details live in [apps/data-platform/README.md](apps/data-platform/README.md).
