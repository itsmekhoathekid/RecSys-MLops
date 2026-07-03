# Load Test Autoscaling

This guide shows how to load test serving autoscaling on GCP.

The current FastAPI autoscaling path uses KEDA `ScaledObject` resources with Prometheus metrics. Prometheus scrapes the FastAPI metrics endpoint, then KEDA scales each Deployment from request rate and p95 latency.

## Current Scaling Targets

| Workload | Namespace | HPA | Min | Max | Scaling signal |
|---|---|---|---:|---:|---|
| `recsys-api-serving` | `api-serving` | `keda-hpa-recsys-api-serving` | 1 | 3 | `/recommendations` req/s and p95 latency |
| `recsys-online-feature-api` | `api-serving` | `keda-hpa-recsys-online-feature-api` | 1 | 3 | `/online-features` req/s and p95 latency |
| `recsys-bst-triton-predictor` | `kserve-triton-inference` | `recsys-bst-triton-predictor` | 1 | 3 | CPU utilization, 15% proof target |

The proof override is in:

```bash
infra/helm/recsys-serving/values-gcp-autoscale-proof.yaml
```

## 1. Start Monitors

Open separate terminals before running the load tests.

```bash
kubectl get hpa -n api-serving -w
```

```bash
kubectl get deploy -n api-serving recsys-api-serving recsys-online-feature-api -w
```

```bash
kubectl get hpa -n kserve-triton-inference -w
```

```bash
kubectl get deploy -n kserve-triton-inference recsys-bst-triton-predictor -w
```

Optional pod monitors:

```bash
kubectl get pods -n api-serving -l app.kubernetes.io/name=recsys-api-serving -w
```

```bash
kubectl get pods -n api-serving -l app.kubernetes.io/name=recsys-online-feature-api -w
```

## 2. Verify Autoscale Objects

```bash
kubectl get scaledobject -n api-serving
kubectl get hpa -n api-serving
kubectl get hpa -n kserve-triton-inference
```

Expected objects:

```text
recsys-api-serving-prometheus           True   False   1   3
recsys-online-feature-api-prometheus    True   False   1   3
```

Describe the FastAPI scalers:

```bash
kubectl describe scaledobject -n api-serving recsys-api-serving-prometheus
kubectl describe scaledobject -n api-serving recsys-online-feature-api-prometheus
```

## 3. Smoke Test

This validates the full path:

```text
FastAPI recommendation API -> online feature API -> Feast Redis online store -> Triton gRPC inference
```

```bash
kubectl -n api-serving exec deploy/recsys-api-serving -c api -- \
  python -c 'import requests, json; r=requests.post("http://127.0.0.1:8080/recommendations", json={"user_id":4,"candidate_item_ids":[1,2,3,4,5],"top_k":3}, timeout=30); print(r.status_code); print(json.dumps(r.json(), indent=2)[:2000]); r.raise_for_status()'
```

## 4. Run Locust To Trigger API And Online Feature API Scaling

This command port-forwards `svc/recsys-api-serving`, runs Locust against `/recommendations`, then prints the HPA and Deployment state before and after the test.

```bash
LOCUST_USERS=30 \
LOCUST_SPAWN_RATE=10 \
LOCUST_DURATION=90s \
RECSYS_LOAD_TARGET=api \
RECSYS_USER_ID=4 \
RECSYS_CANDIDATE_COUNT=20 \
RECSYS_TOP_K=10 \
make serving-autoscale-load-test
```

The `/recommendations` endpoint calls the online feature API internally, so this single run should scale both:

```text
recsys-api-serving: 1/1 -> 3/3
recsys-online-feature-api: 1/1 -> 3/3
```

For a lighter repeatable proof after pods are already warm:

```bash
LOCUST_USERS=15 \
LOCUST_SPAWN_RATE=5 \
LOCUST_DURATION=60s \
RECSYS_LOAD_TARGET=api \
RECSYS_USER_ID=4 \
RECSYS_CANDIDATE_COUNT=10 \
RECSYS_TOP_K=5 \
make serving-autoscale-load-test
```

To stress only the online feature API:

```bash
NAMESPACE=api-serving \
SERVICE=recsys-online-feature-api \
LOCAL_PORT=18089 \
LOCUST_USERS=20 \
LOCUST_SPAWN_RATE=10 \
LOCUST_DURATION=90s \
RECSYS_LOAD_TARGET=feature \
RECSYS_USER_ID=4 \
RECSYS_CANDIDATE_COUNT=20 \
RECSYS_TOP_K=10 \
make serving-autoscale-load-test
```

## 5. Optional Triton Stress Test

Triton/KServe scales by CPU. Normal API traffic may not always push Triton CPU high enough because the model is small, but the chart is configured with `maxReplicas: 3` and a low proof target.

```bash
kubectl get hpa -n kserve-triton-inference
kubectl describe hpa -n kserve-triton-inference recsys-bst-triton-predictor
```

If direct Triton pressure is needed, port-forward the Triton predictor service and run the `triton` Locust target:

```bash
kubectl -n kserve-triton-inference port-forward svc/recsys-bst-triton-predictor 18090:80
```

```bash
RECSYS_LOAD_TARGET=triton \
RECSYS_CANDIDATE_COUNT=200 \
uv run --with locust locust \
  -f tests/load/locustfile_serving.py \
  --host http://127.0.0.1:18090 \
  --headless \
  -u 60 \
  -r 20 \
  -t 2m \
  --only-summary
```

## 6. Capture Evidence

Use these commands during and after the load test.

```bash
kubectl get hpa -n api-serving
kubectl get scaledobject -n api-serving
kubectl get deploy -n api-serving recsys-api-serving recsys-online-feature-api -o wide
kubectl get pods -n api-serving -o wide
```

```bash
kubectl get hpa -n kserve-triton-inference
kubectl get deploy -n kserve-triton-inference recsys-bst-triton-predictor -o wide
```

Good screenshots to capture:

1. Locust summary.
2. FastAPI HPA targets above the request-rate or latency threshold.
3. `recsys-api-serving` at `3/3`.
4. `recsys-online-feature-api` at `3/3`.
5. HPA target back to low values after cooldown.

## Troubleshooting

If HPA `TARGETS` stays `0`, confirm Prometheus can see FastAPI metrics:

```bash
kubectl -n observability exec deploy/recsys-prometheus -- \
  wget -qO- 'http://127.0.0.1:9090/api/v1/query?query=sum(rate(recsys_api_requests_total{namespace="api-serving"}[1m]))'
```

If KEDA reports Prometheus connection errors, check the mesh exception that allows KEDA to query Prometheus:

```bash
kubectl get peerauthentication -n observability recsys-prometheus-keda-permissive
kubectl get authorizationpolicy -n observability recsys-prometheus-keda-allow
```

If pods remain `Pending`, the GKE node pool does not have enough schedulable CPU or memory for the requested replica count. The proof values reduce FastAPI request sizes and Istio sidecar requests so both FastAPI services can reach 3 pods on the coursework cluster.
