# Infrastructure As Code Proof: RecSys MLOps On GCP

This document records the final Infrastructure as Code setup deployed to Google Cloud for the coursework project.

## Target Project

- GCP project: `fsds-coursework`
- Project number: `455131526306`
- Region: `asia-southeast1`
- Zone: `asia-southeast1-b`
- GKE cluster: `recsys-mlops-gke`
- Artifact Registry: `asia-southeast1-docker.pkg.dev/fsds-coursework/recsys`

## IaC Layout

The IaC is split by cloud resource and application service boundary:

```text
infra/
  cloudbuild/
    recsys-images.yaml          # Cloud Build image pipeline, no local Docker dependency
  helm/
    datahub-local/              # DataHub values for GKE deployment
    mlflow-stack/               # MLflow, MinIO model store, Postgres
    recsys-ci/                  # Jenkins CI controller and in-cluster registry
    recsys-data-platform/       # Kafka, Redis, MinIO, Flink, Airflow, Postgres
    recsys-gateway/             # Ingress for API serving
    recsys-observability/       # Prometheus, Grafana, Loki, Tempo, exporters
    recsys-runtime/             # Kubeflow/KFP runtime resources
    recsys-security/            # Istio mTLS and service-to-service authorization policies
    recsys-serving/             # KServe, Triton, FastAPI serving
  terraform/gcp/
    apis.tf                     # Required Google APIs
    cloudbuild.tf               # Cloud Build IAM
    datahub.tf                  # DataHub secrets, Kafka alias, Helm releases
    gke.tf                      # GKE cluster and node pools
    locals.tf                   # image paths, node placement, Helm overrides
    namespaces.tf               # Kubernetes namespaces
    network.tf                  # VPC and subnet
    registry_storage.tf         # Artifact Registry and GCS buckets
    recsys_services.tf          # Helm releases for the RecSys stack
    variables.tf                # deployment toggles and node sizing
```

Terraform provisions the cloud resources, installs required controllers, creates namespaces, and deploys local Helm charts. This makes the setup reproducible from IaC instead of relying on manual Kubernetes setup.

## Cloud Build Image Proof

Images were built on GCP Cloud Build, not local Docker.

```bash
gcloud builds submit \
  --config infra/cloudbuild/recsys-images.yaml \
  --project fsds-coursework
```

Observed build:

```text
id: 57803ccf-e553-417e-a850-9956fe80b9cb
status: SUCCESS
startTime: 2026-06-30T06:00:07.928564650Z
finishTime: 2026-06-30T06:16:09.428610Z
logUrl: https://console.cloud.google.com/cloud-build/builds/57803ccf-e553-417e-a850-9956fe80b9cb?project=455131526306
```

Images pushed with tag `gcp`:

```text
recsys-base-python:gcp      sha256:d16b1f10d013ff9a92ec3f2a2ed4bc9cf062fb378916c84dcbfe8eda69098033
recsys-dataflow-cli:gcp     sha256:b4773a4da53632965a5c6d2e8b7567c1c2da55a6d9d0ca8774fa7531bef2720d
recsys-mlops-training:gcp   sha256:f411e3a3e8255d07d32445d568f741d1c1e1e461f63a88a9a871dc3b12a823d9
recsys-api-serving:gcp      sha256:897999283cf5918f3cf5f421e481b77c4e76fa348ee209369c438f5531b8ded0
recsys-kafka-connect:gcp    sha256:46b24a01b916f561e5e7ea28a750391e380e558fc1fe3272b088bff5e60dfe14
recsys-mlflow:gcp           sha256:fe22a1575c28c42e987e1587e67c6607cbe064da5a91064a822ab9a08c117510
recsys-airflow:gcp          sha256:d1b02369c910f478b4e1ee8e08a12dee07f3be54f9fd3d4634339fde90e262f7
recsys-spark:gcp            sha256:98c30e2c9562b7150e530782ebf53a302d3b6bc99dab90d555abcfb253608b60
recsys-flink:gcp            sha256:ca89c5f434828c619a37c79366943a0a49be98b38d8d379fd9a55e6df44187ce
```

### Image Proof


![Cloud Build proof](../../pngs/gcp_build_log.png)

## Terraform Proof

Terraform was applied from `infra/terraform/gcp` against project `fsds-coursework`.

Validation:

```bash
terraform -chdir=infra/terraform/gcp validate
```

Observed result:

```text
Success! The configuration is valid.
```

### Image proof

![Cloud Build proof](../../pngs/validate_config_tf.png)

Final convergence check:

```bash
terraform -chdir=infra/terraform/gcp plan -detailed-exitcode -no-color
```

Observed result:

```text
No changes. Your infrastructure matches the configuration.

Terraform has compared your real infrastructure against your configuration
and found no differences, so no changes are needed.
```

### Image proof 

![Cloud Build proof](../../pngs/convergence_proof.png)

## GKE Node Split

The cluster is intentionally split into two active node pools:

```bash
kubectl get nodes -L recsys.ai/workload,recsys.ai/pool,node.kubernetes.io/instance-type
```

Observed result:

```text
NAME                                                  STATUS   VERSION               WORKLOAD        POOL           INSTANCE-TYPE
gke-recsys-mlops-gke-recsys-mlops-cpu-d4791f44-714z   Ready    v1.35.5-gke.1163012   data-platform   cpu-services   e2-standard-8
gke-recsys-mlops-gke-recsys-mlops-ml--f31561e0-ltbt   Ready    v1.35.5-gke.1163012   ml-system       ml-system      e2-standard-4
```

Node pool service placement:

Kubernetes `Service` objects are virtual network front doors and are not
scheduled to a node by themselves. The node split is therefore proven by where
the backing pods run. The services below are grouped by the node pool selected
by their pod `nodeSelector`/toleration policy.

| Node pool | Scheduled service/workload group | Kubernetes services and pods behind it | Why it runs there |
| --- | --- | --- | --- |
| `cpu-services` | Data ingestion and feature data platform | `source-postgres`, `airflow-postgres`, `feature-postgres`, `data-platform-minio`, `kafka`, `kafka-connect`, `redis`, `flink-jobmanager`, `flink-taskmanager`, `airflow-webserver`, `airflow-scheduler`, `realtime-event-producer`, `realtime-flink-online-store`, `realtime-flink-offline-store` | Keeps streaming, batch orchestration, feature-store writes, and data-generator traffic on the larger CPU data-platform node. |
| `cpu-services` | Observability, gateway, governance, CI/CD, and control plane | `recsys-grafana`, `recsys-prometheus`, `recsys-loki`, `recsys-tempo`, `recsys-pushgateway`, exporters, `datahub-frontend`, `datahub-gms`, Jenkins `recsys-jenkins`, in-cluster registry `recsys-registry`, NGINX ingress controller, KEDA, cert-manager, KServe controller, Istio control plane, Kubeflow Pipelines control services | These are platform/control-plane services. They support the whole stack and do not need to consume capacity on the isolated ML serving node. |
| `ml-system` | Experiment tracking and model store | MLflow service `mlflow`, MLflow Postgres service `postgres`, MLflow MinIO service `minio`, MinIO bucket init job | Terraform applies the shared `ml-system` node selector/toleration to the MLflow Helm release so experiment tracking and model artifacts stay close to model serving. |
| `ml-system` | Online serving APIs | FastAPI recommendation service `recsys-api-serving`, FastAPI online feature service `recsys-online-feature-api`, KEDA HTTP targets/ScaledObjects for those services | API pods are pinned to the tainted ML node so online inference traffic is isolated from Kafka/Flink/Airflow load. |
| `ml-system` | Model inference runtime | KServe `InferenceService` `recsys-bst-triton`, optional candidate `recsys-bst-triton-candidate`, predictor services `recsys-bst-triton-predictor` and `recsys-bst-triton-candidate-predictor` exposing Triton HTTP and gRPC ports | Triton/KServe predictor pods are pinned to the ML node. The recommendation API calls Triton through the predictor service gRPC port `9000` and receives promoted model updates from the KServe CD flow. |

The placement is defined in Terraform/Helm:

- [gke.tf (line 97)](../../../infra/terraform/gcp/gke.tf#L97), [gke.tf (line 191)](../../../infra/terraform/gcp/gke.tf#L191): defines the `cpu-services` and tainted `ml-system` node pools.
- [locals.tf (line 40)](../../../infra/terraform/gcp/locals.tf#L40), [locals.tf (line 85)](../../../infra/terraform/gcp/locals.tf#L85): reusable ML node selectors and tolerations for ML and serving workloads.
- [recsys_services.tf (line 44)](../../../infra/terraform/gcp/recsys_services.tf#L44), [recsys_services.tf (line 111)](../../../infra/terraform/gcp/recsys_services.tf#L111), [recsys_services.tf (line 158)](../../../infra/terraform/gcp/recsys_services.tf#L158), [recsys_services.tf (line 223)](../../../infra/terraform/gcp/recsys_services.tf#L223): applies ML placement to MLflow, APIs, KServe/Triton, and Ray releases.

The `ml-system` node pool has a taint:

```text
recsys.ai/workload=ml-system:NoSchedule
```

Only workloads with the matching toleration and node selector are scheduled there. This is why API serving, online feature lookup, MLflow, the model-store MinIO, MLflow Postgres, and Triton/KServe predictors are placed on the ML node.

Some Kubernetes and GKE DaemonSet pods run on both nodes, such as logging, metrics, networking, DNS, storage, and metadata agents. That is expected for per-node system services.

### Image Proof

![GKE node proof](../../pngs/tf_get_nodes.png)

### All Services's Namespace up and running on GCP

![Helm release proof](../../pngs/svcs_ns.png)

Namespace meaning:

| Namespace | Purpose | Main workloads/services |
| --- | --- | --- |
| `api-serving` | Public/API serving layer for online feature lookup and recommendation inference orchestration. Istio injection is enabled here. | FastAPI `recsys-online-feature-api`, FastAPI `recsys-api-serving`, RecSys API ingress/gateway resources, KEDA HTTP autoscaling target. |
| `kserve-triton-inference` | Model inference runtime layer. Istio injection is enabled here. | KServe `InferenceService`, Triton predictor pod, Triton HTTP/gRPC services, MinIO model-store service account/secret. |
| `experiment-tracking` | ML experiment tracking and model registry/storage layer. Istio injection is enabled here. | MLflow, MLflow MinIO model store, MLflow Postgres, model-store bucket initialization job. |
| `recsys-dataflow` | Data platform and feature pipeline runtime. Istio injection is enabled here. | Kafka, Zookeeper, Kafka Connect, Redis online store, Flink, Airflow, source Postgres, data-platform MinIO, realtime producer/consumer. |
| `datahub` | Metadata governance and lineage layer. | DataHub frontend, DataHub GMS, OpenSearch, MySQL prerequisites, `kafka` ExternalName alias to the data platform Kafka service. |
| `ci` | CI/CD execution layer for coursework proof runs. | Jenkins controller, Docker-in-Docker sidecar, in-cluster Docker registry, registry node proxy, Jenkins home PVC, registry PVC. |
| `observability` | Metrics, logs, traces, and ML/data monitoring layer. Istio injection is enabled here. | Prometheus, Grafana, Loki, Tempo, Promtail, PushGateway, Redis/Postgres exporters. |
| `kubeflow` | ML workflow orchestration layer. | Kubeflow Pipelines API/UI/controllers, workflow controller, KubeRay operator, metadata services, MySQL, SeaweedFS. |
| `istio-system` | Service mesh control plane. | `istiod`, Istio base CRDs/webhooks, sidecar injection and mTLS control plane. |
| `recsys-security` | RecSys security policy release namespace. | Helm release that installs Istio `PeerAuthentication` and `AuthorizationPolicy` resources for service-to-service authorization. |
| `ingress-nginx` | External HTTP/HTTPS entrypoint. | NGINX ingress controller LoadBalancer with external IP `34.21.171.234`. |
| `keda` | Event-driven autoscaling layer. | KEDA operator, KEDA HTTP add-on controller/interceptor/scaler. |
| `cert-manager` | Certificate and webhook support layer. | cert-manager controller, cainjector, and webhook used by platform controllers such as KServe. |
| `kserve` | KServe controller namespace. | KServe controller manager and local model controller. Sidecar injection is disabled for the controller namespace to keep control-plane webhooks stable. |
| `gmp-system` / `gmp-public` | Google Managed Prometheus system namespaces. | GKE/GMP collectors and operator-managed monitoring components. |
| `kube-system` and other `gke-managed-*` namespaces | GKE-managed cluster system namespaces. | DNS, networking, CSI storage, metadata server, logging/metrics agents, node system DaemonSets. |

### Node `cpu-services` services

![Helm release proof](../../pngs/mlops_cpu_node_.png)

**Figure: `cpu-services` node proof.** This image should show the data platform and control-plane pods on the CPU node: Kafka, Redis, Flink, Airflow, data-platform Postgres/MinIO, DataHub, observability, Jenkins, gateway, Kubeflow/KServe controllers, and GKE system DaemonSets.

### Node `ml-system` services

![Helm release proof](../../pngs/ml_node_pods.png)

**Figure: `ml-system` node proof.** This image should show the isolated ML/runtime pods on the tainted ML node: MLflow, MLflow Postgres, MLflow MinIO model store, `recsys-api-serving`, `recsys-online-feature-api`, and the KServe/Triton predictor pods. Some per-node DaemonSet pods can also appear here because logging, metrics, mesh, networking, and storage agents must run on every node.

## Helm Release Proof

```bash
helm list -A
```

Observed deployed releases:

```text
cert-manager          cert-manager             deployed
datahub               datahub                  deployed
ingress-nginx         ingress-nginx            deployed
keda                  keda                     deployed
keda-add-ons-http     keda                     deployed
istio-base            istio-system             deployed
istiod                istio-system             deployed
kuberay-operator      kubeflow                 deployed
prerequisites         datahub                  deployed
recsys-data-platform  recsys-dataflow          deployed
recsys-gateway        api-serving              deployed
recsys-mlflow         experiment-tracking      deployed
recsys-observability  observability            deployed
recsys-runtime        kubeflow                 deployed
recsys-security       recsys-security          deployed
recsys-serving        kserve-triton-inference  deployed
```

This proves the full coursework MLOps stack is installed through Terraform-managed Helm releases, including DataHub, data platform, observability, runtime, gateway, service mesh, API serving, and KServe/Triton.

### Image Proof

![Helm release proof](../../pngs/helm_list___.png)
