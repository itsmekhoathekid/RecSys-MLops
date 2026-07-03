# Autoscaling Evidence

## Autoscaling Configuration Evidence

### Online Feature API Autoscaling

#### Code References

- [infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml line 29](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L29): enables Prometheus autoscaling for `recsys-online-feature-api`.
- [infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml line 36](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L36): sets min/max replicas to `1 -> 3`.
- [infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml line 38](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L38): request-rate threshold for `/online-features`.
- [infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml line 42](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L42): latency threshold for `/online-features`.
- [infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml line 40](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L40): renders the online feature API KEDA `ScaledObject`.
- [infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml line 70](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L70): Prometheus query for online feature API request rate.
- [infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml line 77](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L77): Prometheus query for online feature API latency.

#### Configuration

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

#### Scaling Behavior

`recsys-online-feature-api` scales from 1 to 3 pods. KEDA reads Prometheus metrics for `/online-features` and scales up when either request rate is above 4 req/s or average request latency is above 0.08 seconds over a 1-minute window. This service is expected to scale together with `recsys-api-serving` because every recommendation request fetches online Feast features before inference.

### Recommendation API Autoscaling

#### Code References

- [apps/api-serving/src/inference_api.py line 60](../../../apps/api-serving/src/inference_api.py#L60): recommendation endpoint `/recommendations`.
- [apps/api-serving/src/inference_api.py line 68](../../../apps/api-serving/src/inference_api.py#L68): recommendation API calls the online feature API before Triton inference.
- [infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml line 12](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L12): enables Prometheus autoscaling for `recsys-api-serving`.
- [infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml line 19](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L19): sets min/max replicas to `1 -> 3`.
- [infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml line 21](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L21): request-rate threshold for `/recommendations`.
- [infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml line 25](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L25): latency threshold for `/recommendations`.
- [infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml line 2](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L2): renders the API serving KEDA `ScaledObject`.
- [infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml line 31](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L31): Prometheus query for API serving request rate.
- [infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml line 38](../../../infra/helm/recsys-serving/templates/fastapi-prometheus-scaledobjects.yaml#L38): Prometheus query for API serving latency.

#### Configuration

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

#### Scaling Behavior

`recsys-api-serving` scales from 1 to 3 pods. KEDA reads Prometheus metrics for `/recommendations` and scales up when either request rate is above 4 req/s or average request latency is above 0.15 seconds over a 1-minute window. This is the public serving entrypoint, so load starts here and then propagates to the online feature API and Triton inference.

### Triton Inference Autoscaling

#### Code References

- [infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml line 46](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L46): enables Triton/KServe resource autoscaling.
- [infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml line 48](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L48): sets min/max replicas to `1 -> 3`.
- [infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml line 52](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L52): CPU autoscale metric.
- [infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml line 56](../../../infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml#L56): lowers Triton resource request for scale-up proof on the coursework cluster.
- [infra/helm/recsys-serving/templates/kserve-resource-scaledobject.yaml line 1](../../../infra/helm/recsys-serving/templates/kserve-resource-scaledobject.yaml#L1): renders the Triton/KServe KEDA `ScaledObject`.
- [infra/helm/recsys-serving/templates/kserve-resource-scaledobject.yaml line 17](../../../infra/helm/recsys-serving/templates/kserve-resource-scaledobject.yaml#L17): Triton min/max replica fields.
- [infra/helm/recsys-serving/templates/kserve-resource-scaledobject.yaml line 26](../../../infra/helm/recsys-serving/templates/kserve-resource-scaledobject.yaml#L26): Triton CPU trigger.

#### Configuration

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

#### Scaling Behavior

`recsys-bst-triton-predictor` scales from 1 to 3 pods using CPU utilization. The proof target is 15% CPU utilization, and the request CPU is lowered to `100m` so the small coursework model can still demonstrate scale-up on the limited GKE node. Triton receives traffic indirectly from `recsys-api-serving` after the API builds the inference payload from online features.

## Load Test Evidence

### Locust Stress Test Command

Code references:

- [infra/k8s/scripts/serving_autoscale_load_test.sh line 4](../../../infra/k8s/scripts/serving_autoscale_load_test.sh#L4): load-test script target namespace/service defaults.
- [infra/k8s/scripts/serving_autoscale_load_test.sh line 23](../../../infra/k8s/scripts/serving_autoscale_load_test.sh#L23): port-forwards the selected Kubernetes Service.
- [infra/k8s/scripts/serving_autoscale_load_test.sh line 27](../../../infra/k8s/scripts/serving_autoscale_load_test.sh#L27): prints initial HPA/ScaledObject/deployment state.
- [infra/k8s/scripts/serving_autoscale_load_test.sh line 34](../../../infra/k8s/scripts/serving_autoscale_load_test.sh#L34): runs Locust with the selected load target.
- [infra/k8s/scripts/serving_autoscale_load_test.sh line 47](../../../infra/k8s/scripts/serving_autoscale_load_test.sh#L47): prints autoscale state after load.
- [tests/load/locustfile_serving.py line 20](../../../tests/load/locustfile_serving.py#L20): selects the `api` load target for the end-to-end serving path.
- [tests/load/locustfile_serving.py line 45](../../../tests/load/locustfile_serving.py#L45): `api` target calls `/recommendations`.
- [apps/api-serving/src/inference_api.py line 68](../../../apps/api-serving/src/inference_api.py#L68): API serving calls the online feature API.
- [apps/api-serving/src/inference_api.py line 75](../../../apps/api-serving/src/inference_api.py#L75): API serving sends the feature payload to the ranking path backed by Triton.

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
