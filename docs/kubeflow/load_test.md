# Load Test Autoscaling

This guide shows how to load test autoscaling for:

- FastAPI serving in namespace `api-serving`
- Triton/KServe inference in namespace `kserve-triton-inference`

The FastAPI service uses KEDA HTTP request-rate autoscaling. Requests must go through the KEDA HTTP interceptor so KEDA can count API request rate. Requests sent directly to the internal Kubernetes Service, such as `http://recsys-api-serving`, still reach the app, but they bypass KEDA HTTP metrics and will not trigger API request-rate scaling.

Triton/KServe uses KEDA resource autoscaling on CPU utilization. This is the correct path for the current serving design because FastAPI calls Triton through gRPC directly.

## Current Scaling Targets

| Workload | Namespace | HPA | Min | Max | Scaling target |
|---|---|---|---:|---:|---:|
| `recsys-api-serving` | `api-serving` | `keda-hpa-recsys-api-serving-http` | 1 | 5 | 5 req/s |
| `recsys-bst-triton-predictor` | `kserve-triton-inference` | `recsys-bst-triton-predictor` | 1 | 3 | 50% CPU utilization |

## 1. Start Monitors

CPU-based Triton autoscaling requires the Kubernetes metrics API. On minikube, enable `metrics-server` first:

```bash
minikube -p recsys-mlops addons enable metrics-server
```

Verify CPU metrics:

```bash
kubectl top pods -n kserve-triton-inference
```

Open separate terminals before running the load tests.

```bash
kubectl get hpa -A -w
```

```bash
kubectl get deploy -n api-serving recsys-api-serving -w
```

```bash
kubectl get deploy -n kserve-triton-inference recsys-bst-triton-predictor -w
```

Optional pod monitors:

```bash
kubectl get pods -n api-serving -l app.kubernetes.io/name=recsys-api-serving -w
```

```bash
kubectl get pods -n kserve-triton-inference -l app=isvc.recsys-bst-triton-predictor -w
```

## 2. Port-Forward KEDA HTTP Interceptor

Run this in a separate terminal and keep it running during the load tests.

```bash
kubectl -n keda port-forward svc/keda-add-ons-http-interceptor-proxy 18081:8080
```

If the service name is different, inspect services first:

```bash
kubectl get svc -n keda
```

Use the service with `interceptor-proxy` in its name.

## 3. Smoke Test API Through KEDA

This request must return `HTTP/1.1 200 OK`.

```bash
curl -i -X POST http://127.0.0.1:18081/recommendations \
  -H 'Host: recsys-api-serving.local' \
  -H 'Content-Type: application/json' \
  -d '{"user_id":50,"candidate_item_ids":[456,379,287,194,157],"top_k":3}'
```

Expected response shape:

```json
{
  "user_id": 50,
  "model_version": "v2",
  "items": [
    {"item_id": 157, "score": 0.5533815622329712}
  ]
}
```

## 4. E2E Load Test For Both API And Triton

This test sends traffic to `recsys-api-serving.local` through KEDA HTTP interceptor. FastAPI then fetches online features and calls Triton through gRPC. This single test should trigger:

- API scale-up from KEDA HTTP request rate
- Triton scale-up from CPU utilization caused by real gRPC inference traffic

`RECSYS_CANDIDATE_COUNT=200` makes each recommendation request heavier by sending 200 candidate items to Triton.

```bash
RECSYS_LOAD_TARGET=api \
RECSYS_HOST_HEADER=recsys-api-serving.local \
RECSYS_CANDIDATE_COUNT=200 \
RECSYS_TOP_K=10 \
uv run --with locust locust \
  -f tests/load/locustfile_serving.py \
  --host http://127.0.0.1:18081 \
  --headless \
  -u 160 \
  -r 40 \
  -t 3m \
  --only-summary
```

Expected scale behavior:

```text
recsys-api-serving: 1/1 -> up to 5/5 -> 1/1 after cooldown
recsys-bst-triton-predictor: 1/1 -> up to 3/3 -> 1/1 after cooldown
```

Expected HPA signals during load:

```text
api-serving  keda-hpa-recsys-api-serving-http  ...  TARGETS > 5  MINPODS 1  MAXPODS 5
kserve-triton-inference  recsys-bst-triton-predictor  ...  CPU target above 50%  MINPODS 1  MAXPODS 3
```

Example observed E2E result:

```text
POST api:/recommendations
3804 requests
0 fails
21.50 req/s
```

## 5. Optional Direct Triton HTTP Stress Test

This path is optional after switching Triton to CPU autoscaling. The preferred proof is the E2E API load test above because it exercises the real application path:

```text
Locust -> KEDA HTTP interceptor -> FastAPI -> Redis -> Triton gRPC
```

If you still want to stress Triton directly with the Locust `triton` target, expose the Triton HTTP service and run:

```bash
RECSYS_LOAD_TARGET=triton \
uv run --with locust locust \
  -f tests/load/locustfile_serving.py \
  --host http://127.0.0.1:<TRITON_HTTP_PORT> \
  --headless \
  -u 60 \
  -r 20 \
  -t 2m \
  --only-summary
```

Example observed result:

```text
POST triton:/v2/models/bst_ensemble/infer
16245 requests
0 fails
135.59 req/s
```

## 6. Capture Evidence

Use these commands during and after each load test.

```bash
kubectl get hpa -A
```

```bash
kubectl get deploy -n api-serving recsys-api-serving -o wide
```

```bash
kubectl get deploy -n kserve-triton-inference recsys-bst-triton-predictor -o wide
```

```bash
kubectl describe hpa -n api-serving keda-hpa-recsys-api-serving-http
```

```bash
kubectl describe hpa -n kserve-triton-inference recsys-bst-triton-predictor
```

Good screenshots to capture:

1. Locust summary with `0 fails`.
2. HPA target above threshold during load.
3. Deployment replicas scaled up.
4. HPA target back to `0`.
5. Deployment replicas scaled down after cooldown.

## Important Note About API-To-Triton Traffic

The FastAPI `/recommendations` endpoint calls Triton with gRPC directly:

```text
recsys-bst-triton-predictor.kserve-triton-inference.svc.cluster.local:9000
```

That gRPC call bypasses the KEDA HTTP interceptor. Therefore Triton should not rely on KEDA HTTP request-rate metrics for this path. The chart now uses CPU resource autoscaling for Triton, so real gRPC inference load can scale the Triton deployment.

With the current design:

- Load on `/recommendations` scales `recsys-api-serving` by API request rate.
- The same load can scale `recsys-bst-triton-predictor` by CPU utilization from gRPC inference.
- No separate Triton HTTP KEDA path is required for the normal API serving flow.

## Troubleshooting

If Locust shows `100%` failures, first test one request:

```bash
curl -i -X POST http://127.0.0.1:18081/recommendations \
  -H 'Host: recsys-api-serving.local' \
  -H 'Content-Type: application/json' \
  -d '{"user_id":50,"candidate_item_ids":[456,379,287,194,157],"top_k":3}'
```

Common causes:

- `Connection refused`: port-forward is not running.
- `404`: wrong `Host` header or no matching `HTTPScaledObject`.
- `502` or `503`: KEDA routed the request, but the backend service or Triton dependency is not ready.
- HPA `TARGETS` stays `0`: traffic is not being counted by KEDA HTTP metrics.

If scale-down is slow, inspect HPA:

```bash
kubectl describe hpa -n api-serving keda-hpa-recsys-api-serving-http
```

```bash
kubectl describe hpa -n kserve-triton-inference recsys-bst-triton-predictor
```

The condition below is normal after a traffic spike:

```text
ScaleDownStabilized
recent recommendations were higher than current one
```

It means HPA is intentionally delaying scale-down to avoid rapid replica flapping.
