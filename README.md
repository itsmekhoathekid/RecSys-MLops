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

To stop and clean up the full-service stack:

```bash
make cluster-down
```

`cluster-down` uninstalls the RecSys Helm releases, deletes full-service namespaces, verifies they are gone, then stops the Minikube profile. To delete the Minikube profile entirely:

```bash
RECSYS_CLUSTER_DELETE_PROFILE=1 make cluster-down
```

## I coded whatever you see in this diagram

![Data & ML system](docs/pngs/full_flow.png)

# Data platform

![Data platform](docs/pngs/data_flow.png)
