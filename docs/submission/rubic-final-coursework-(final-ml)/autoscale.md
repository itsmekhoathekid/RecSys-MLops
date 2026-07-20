# Autoscaling Evidence

## Autoscaling Configuration Evidence

### End-To-End Autoscaling Flow

```text
Locust / client
  -> POST /recommendations
  -> Recommendation API records request count and latency
  -> GET /metrics exposes the in-memory metric snapshot
  -> Prometheus scrapes annotated API pods
  -> KEDA evaluates PromQL request-rate and latency triggers
  -> Kubernetes HPA changes the Recommendation API replica count
  -> Recommendation API calls Online Feature API and Triton
  -> Online Feature API follows the same Prometheus/KEDA/HPA path
  -> Triton follows a separate KEDA CPU/HPA path
  -> GKE adds nodes only when newly requested pods cannot be scheduled
```

Reference code:

- [inference_api.py (line 75)](../../../apps/api-serving/src/inference_api.py#L75): public `/recommendations` entrypoint.
- [api_runtime.py (line 18)](../../../apps/api-serving/src/api_runtime.py#L18), [observability.py (line 199)](../../../apps/api-serving/src/observability.py#L199): request middleware and metric recording.
- [api_runtime.py (line 65)](../../../apps/api-serving/src/api_runtime.py#L65): Prometheus text endpoint returned by `/metrics`.
- [prometheus.yaml (line 94)](../../../infra/helm/recsys-observability/templates/prometheus.yaml#L94): annotated-pod discovery and scraping.
- [fastapi-prometheus-scaledobjects.yaml (line 1)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L1): API and feature-API KEDA Prometheus scalers.
- [kserve-resource-scaledobject.yaml (line 1)](../../../infra/helm/recsys-serving/templates/kserve-resource-scaledobject.yaml#L1): Triton KEDA CPU scaler.
- [gke.tf (line 97)](../../../infra/terraform/gcp/gke.tf#L97): independent GKE node-pool autoscaling.

### Shared API Metric Emission

Every FastAPI request passes through the shared middleware. The middleware records request count and elapsed time even when the request fails.

```python
@app.middleware("http")
async def metrics_middleware(request, call_next):
    start = time.perf_counter()
    ...
    duration = time.perf_counter() - start
    observe_request(route, method, status_code, duration)
```

Reference code: [api_runtime.py (line 18)](../../../apps/api-serving/src/api_runtime.py#L18), [api_runtime.py (line 39)](../../../apps/api-serving/src/api_runtime.py#L39).

The emitted series include the `service`, `route`, and `method` labels required by the KEDA PromQL selectors.

```python
request_labels = {
    "service": SERVICE_NAME,
    "route": route,
    "method": method,
}
METRICS.inc("recsys_api_requests_total", labels={**request_labels, "status": str(status)})
METRICS.observe("recsys_api_request_duration_seconds", duration_seconds, request_labels)
```

Reference code: [observability.py (line 199)](../../../apps/api-serving/src/observability.py#L199), [test_serving.py (line 631)](../../../tests/unit/api_serving/test_serving.py#L631).

The API exposes the current process-local metric snapshot as Prometheus text.

```python
async def metrics() -> Response:
    return Response(
        metrics_text(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
```

Reference code: [api_runtime.py (line 65)](../../../apps/api-serving/src/api_runtime.py#L65), [inference_api.py (line 68)](../../../apps/api-serving/src/inference_api.py#L68).

### Prometheus Pod Scraping

The API Deployment marks every API pod as a Prometheus scrape target.

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/path: /metrics
  prometheus.io/port: "8080"
```

Reference code: [api-deployment.yaml (line 28)](../../../infra/helm/recsys-serving/templates/api-deployment.yaml#L28), [feature-api-deployment.yaml (line 29)](../../../infra/helm/recsys-serving/templates/feature-api-deployment.yaml#L29).

The repository's standalone Prometheus discovers those annotated pods and scrapes them every 15 seconds. The rendered `ServiceMonitor` is compatibility metadata; it is not the active scrape path for this standalone Prometheus deployment.

```yaml
global:
  scrape_interval: 15s
scrape_configs:
  - job_name: recsys-kubernetes-pods
    kubernetes_sd_configs:
      - role: pod
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
        action: keep
        regex: "true"
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_path]
        target_label: __metrics_path__
      - source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
        target_label: __address__
```

Reference code: [prometheus.yaml (line 40)](../../../infra/helm/recsys-observability/templates/prometheus.yaml#L40), [prometheus.yaml (line 94)](../../../infra/helm/recsys-observability/templates/prometheus.yaml#L94).

### Shared KEDA Prometheus Settings

```yaml
autoscaling:
  http:
    api:
      enabled: false
  prometheus:
    enabled: true
    serverAddress: http://recsys-prometheus.observability.svc.cluster.local:9090
    pollingInterval: 10
    cooldownPeriod: 120
    restoreToOriginalReplicaCount: true
```

Reference code: [values-gcp-autoscale-proof.yaml (line 1)](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L1), [api-http-scaledobject.yaml (line 1)](../../../infra/helm/recsys-serving/templates/api-http-scaledobject.yaml#L1), [fastapi-prometheus-scaledobjects.yaml (line 1)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L1).

`http.api.enabled=false` disables the KEDA HTTP add-on scaler for the API. `prometheus.enabled=true` selects the KEDA Prometheus scaler, so only one autoscaler controls each API Deployment.

### Recommendation API Autoscaling

#### Desired Configuration

```yaml
autoscaling:
  prometheus:
    api:
      enabled: true
      name: recsys-api-serving-prometheus
      hpaName: recsys-api-serving
      serviceLabel: recsys-api-serving
      route: /recommendations
      method: POST
      minReplicas: 1
      maxReplicas: 3
      requestRate:
        targetValue: "4"
        activationThreshold: "1"
        window: 1m
      latency:
        targetValue: "0.15"
        activationThreshold: "0.04"
        window: 1m
```

Reference code: [values-gcp-autoscale-proof.yaml (line 12)](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L12), [fastapi-prometheus-scaledobjects.yaml (line 2)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L2).

#### Request-Rate PromQL Trigger

```promql
sum(
  rate(
    recsys_api_requests_total{
      service="recsys-api-serving",
      route="/recommendations",
      method="POST"
    }[1m]
  )
)
```

Reference code: [fastapi-prometheus-scaledobjects.yaml (line 25)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L25), [fastapi-prometheus-scaledobjects.yaml (line 31)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L31).

#### Average-Latency PromQL Trigger

```promql
sum(rate(recsys_api_request_duration_seconds_sum{
  service="recsys-api-serving",
  route="/recommendations",
  method="POST"
}[1m]))
/
clamp_min(
  sum(rate(recsys_api_request_duration_seconds_count{
    service="recsys-api-serving",
    route="/recommendations",
    method="POST"
  }[1m])),
  0.001
)
```

Reference code: [fastapi-prometheus-scaledobjects.yaml (line 32)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L32), [fastapi-prometheus-scaledobjects.yaml (line 38)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L38).

#### KEDA To HPA Target

```yaml
kind: ScaledObject
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: recsys-api-serving
  minReplicaCount: 1
  maxReplicaCount: 3
  horizontalPodAutoscalerConfig:
    name: recsys-api-serving
```

Reference code: [fastapi-prometheus-scaledobjects.yaml (line 3)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L3), [fastapi-prometheus-scaledobjects.yaml (line 12)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L12), [api-deployment.yaml (line 9)](../../../infra/helm/recsys-serving/templates/api-deployment.yaml#L9).

#### Scaling Behavior

`recsys-api-serving` scales from 1 to 3 pods. KEDA supplies two external metrics to the HPA; the HPA uses the larger replica recommendation. The proof targets are 4 requests per second and 0.15-second average latency over a 1-minute window.

### Online Feature API Autoscaling

#### Desired Configuration

```yaml
autoscaling:
  prometheus:
    featureApi:
      enabled: true
      name: recsys-online-feature-api-prometheus
      hpaName: recsys-online-feature-api
      serviceLabel: recsys-online-feature-api
      route: /online-features
      method: POST
      minReplicas: 1
      maxReplicas: 3
      requestRate:
        targetValue: "4"
        activationThreshold: "1"
        window: 1m
      latency:
        targetValue: "0.08"
        activationThreshold: "0.03"
        window: 1m
```

Reference code: [values-gcp-autoscale-proof.yaml (line 29)](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L29), [fastapi-prometheus-scaledobjects.yaml (line 40)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L40).

#### Request-Rate PromQL Trigger

```promql
sum(
  rate(
    recsys_api_requests_total{
      service="recsys-online-feature-api",
      route="/online-features",
      method="POST"
    }[1m]
  )
)
```

Reference code: [fastapi-prometheus-scaledobjects.yaml (line 64)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L64), [fastapi-prometheus-scaledobjects.yaml (line 70)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L70).

#### Average-Latency PromQL Trigger

```promql
sum(rate(recsys_api_request_duration_seconds_sum{
  service="recsys-online-feature-api",
  route="/online-features",
  method="POST"
}[1m]))
/
clamp_min(
  sum(rate(recsys_api_request_duration_seconds_count{
    service="recsys-online-feature-api",
    route="/online-features",
    method="POST"
  }[1m])),
  0.001
)
```

Reference code: [fastapi-prometheus-scaledobjects.yaml (line 71)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L71), [fastapi-prometheus-scaledobjects.yaml (line 77)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L77).

#### KEDA To HPA Target

```yaml
kind: ScaledObject
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: recsys-online-feature-api
  minReplicaCount: 1
  maxReplicaCount: 3
  horizontalPodAutoscalerConfig:
    name: recsys-online-feature-api
```

Reference code: [fastapi-prometheus-scaledobjects.yaml (line 42)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L42), [fastapi-prometheus-scaledobjects.yaml (line 51)](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L51), [feature-api-deployment.yaml (line 11)](../../../infra/helm/recsys-serving/templates/feature-api-deployment.yaml#L11).

#### Scaling Behavior

`recsys-online-feature-api` scales from 1 to 3 pods when either request rate exceeds 4 requests per second or average latency exceeds 0.08 seconds over the 1-minute query window. Recommendation traffic drives this scaler because every recommendation fetches online features before inference.

### Triton Inference Autoscaling

#### KServe External-Autoscaler Handoff

```yaml
metadata:
  annotations:
    serving.kserve.io/deploymentMode: RawDeployment
    serving.kserve.io/autoscalerClass: external
```

Reference code: [values.yaml (line 20)](../../../infra/helm/recsys-serving/values.yaml#L20), [inferenceservice.yaml (line 9)](../../../infra/helm/recsys-serving/templates/inferenceservice.yaml#L9), [inferenceservice.yaml (line 12)](../../../infra/helm/recsys-serving/templates/inferenceservice.yaml#L12).

`RawDeployment` avoids Knative Serving. `autoscalerClass=external` delegates replica ownership to the KEDA-created HPA instead of allowing KServe to compete for the same Deployment scale target.

#### Desired KEDA CPU Configuration

```yaml
autoscaling:
  kserveResource:
    enabled: true
    minReplicas: 1
    maxReplicas: 3
    pollingInterval: 15
    cooldownPeriod: 240
    cpu:
      enabled: true
      metricType: Utilization
      value: "15"
kserve:
  resources:
    requests:
      cpu: 100m
      memory: 768Mi
    limits:
      cpu: "2"
      memory: 4Gi
```

Reference code: [values-gcp-autoscale-proof.yaml (line 46)](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L46), [values-gcp-autoscale-proof.yaml (line 56)](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L56).

#### KEDA To Triton Deployment Target

```yaml
kind: ScaledObject
metadata:
  annotations:
    scaledobject.keda.sh/transfer-hpa-ownership: "true"
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: recsys-bst-triton-predictor
  minReplicaCount: 1
  maxReplicaCount: 3
  triggers:
    - type: cpu
      metricType: Utilization
      metadata:
        value: "15"
```

Reference code: [kserve-resource-scaledobject.yaml (line 1)](../../../infra/helm/recsys-serving/templates/kserve-resource-scaledobject.yaml#L1), [kserve-resource-scaledobject.yaml (line 7)](../../../infra/helm/recsys-serving/templates/kserve-resource-scaledobject.yaml#L7), [kserve-resource-scaledobject.yaml (line 13)](../../../infra/helm/recsys-serving/templates/kserve-resource-scaledobject.yaml#L13), [kserve-resource-scaledobject.yaml (line 26)](../../../infra/helm/recsys-serving/templates/kserve-resource-scaledobject.yaml#L26).

#### Scaling Behavior

`recsys-bst-triton-predictor` scales from 1 to 3 pods using Kubernetes CPU utilization rather than Prometheus request metrics. The proof CPU target is 15%, and the request is reduced to `100m` so the coursework-sized inference workload can demonstrate scale-up.

### GKE Node-Pool Autoscaling

Application autoscaling creates pods; it does not create nodes. If new pods remain Pending because the CPU pool has insufficient capacity, the GKE Cluster Autoscaler can add nodes within its configured bounds.

```hcl
resource "google_container_node_pool" "cpu" {
  node_count = var.cpu_min_nodes

  autoscaling {
    min_node_count = var.cpu_min_nodes
    max_node_count = var.cpu_max_nodes
  }
}
```

Reference code: [gke.tf (line 97)](../../../infra/terraform/gcp/gke.tf#L97), [gke.tf (line 105)](../../../infra/terraform/gcp/gke.tf#L105), [variables.tf (line 79)](../../../infra/terraform/gcp/variables.tf#L79), [variables.tf (line 85)](../../../infra/terraform/gcp/variables.tf#L85).

## Load Test Evidence

### Locust Stress Test Command

Run one end-to-end recommendation API load test. This single command triggers the full serving path:

```text
Locust -> recsys-api-serving -> recsys-online-feature-api -> Triton inference
```

```bash
LOCUST_USERS=60 \
LOCUST_SPAWN_RATE=20 \
LOCUST_DURATION=3m \
RECSYS_LOAD_TARGET=api \
RECSYS_USER_ID=4 \
RECSYS_CANDIDATE_COUNT=200 \
RECSYS_TOP_K=10 \
make serving-autoscale-load-test
```

Reference code:

- [serving_autoscale_load_test.sh (line 1)](../../../infra/k8s/scripts/serving_autoscale_load_test.sh#L1), [serving_autoscale_load_test.sh (line 48)](../../../infra/k8s/scripts/serving_autoscale_load_test.sh#L48): selects the target, port-forwards the Service, prints autoscale state, runs Locust, and prints the post-load state.
- [locustfile_serving.py (line 21)](../../../tests/load/locustfile_serving.py#L21), [locustfile_serving.py (line 90)](../../../tests/load/locustfile_serving.py#L90): selects the load target and calls `/recommendations` or `/online-features`.
- [inference_api.py (line 75)](../../../apps/api-serving/src/inference_api.py#L75), [inference_api.py (line 119)](../../../apps/api-serving/src/inference_api.py#L119): recommendation serving calls the online-feature client and sends the feature payload through the Triton-backed ranking path.

### Baseline Before Load

#### Screenshot Evidence

![Before scaling proof](../../pngs/before_scaling.png)

### Recommendation And Online Feature APIs Scaling Up

#### Screenshot Evidence

![API serving scaling proof](../../pngs/api-serving-scaling-up.png)

![Online feature API scaling proof](../../pngs/online-feature-api-scaling-up.png)

### Triton Inference Scaling Up

#### Screenshot Evidence

![Triton inference scaling proof](../../pngs/triton-inference-scaling-later.png)

### Fully Scaled State

#### Screenshot Evidence

![Fully scaled proof](../../pngs/fully_scaled.png)
