# A/B Testing Proof

This proof covers the final-coursework rubric item **A/B Testing** on GCP/GKE project `fsds-coursework`.

## Scope

| Rubric item | Implementation |
|---|---|
| Perform A/B traffic split for 2 versions of inference service | FastAPI uses `TritonABRouter` to route each user deterministically to control or candidate Triton/KServe gRPC service. |
| Deploy via CI/CD | Model CD renders the same Helm values used by Terraform/Jenkins; the GCP proof is applied through `helm_release.recsys_serving`. |
| Monitor 2 versions | Grafana dashboard `Model A/B Testing` monitors control vs candidate traffic, errors, latency, confidence, score shape, and Triton pod resources. |

Groundtruth assumption: we do not have online groundtruth labels in this proof, so the A/B dashboard uses proxy online metrics: HTTP success/error, empty recommendation rate, model latency, Triton latency, score shape, and confidence distribution.

## Implementation

Code references:

- [apps/api-serving/src/ab_testing.py](../../../apps/api-serving/src/ab_testing.py): `TritonABRouter` assigns users to `control` or `candidate` with a stable hash of `experiment_id:user_id`.
- [apps/api-serving/src/ranking.py](../../../apps/api-serving/src/ranking.py): recommendation flow selects a route, calls the selected ranker, and emits A/B labels.
- [apps/api-serving/src/observability.py](../../../apps/api-serving/src/observability.py): emits `model_predictions_total`, `model_prediction_latency_seconds`, and `model_prediction_confidence`.
- [infra/helm/recsys-serving/templates/inferenceservice.yaml](../../../infra/helm/recsys-serving/templates/inferenceservice.yaml): renders both control and candidate `InferenceService` resources.
- [infra/helm/recsys-serving/templates/api-configmap.yaml](../../../infra/helm/recsys-serving/templates/api-configmap.yaml): passes A/B config into the API pod.
- [infra/helm/recsys-observability/dashboards/model-ab-testing.json](../../../infra/helm/recsys-observability/dashboards/model-ab-testing.json): Grafana dashboard.

Current GCP values:

```text
AB_TEST_ENABLED=1
AB_EXPERIMENT_ID=bst-stable-vs-candidate-20260630
AB_CANDIDATE_WEIGHT_PERCENT=20
AB_CONTROL_MODEL_VERSION=stable-001
AB_CANDIDATE_MODEL_VERSION=candidate-001
AB_CONTROL_TRITON_URL=recsys-bst-triton-grpc.kserve-triton-inference.svc.cluster.local:9000
AB_CANDIDATE_TRITON_URL=recsys-bst-triton-candidate-grpc.kserve-triton-inference.svc.cluster.local:9000
```

## Two Inference Services

```bash
kubectl get inferenceservice -n kserve-triton-inference
kubectl get svc -n kserve-triton-inference
kubectl get deploy,pod -n kserve-triton-inference
```

Observed result:

```text
NAME                          READY
recsys-bst-triton             True
recsys-bst-triton-candidate   True

NAME                                    TYPE        PORT(S)
recsys-bst-triton-grpc                  ClusterIP   9000/TCP
recsys-bst-triton-candidate-grpc        ClusterIP   9000/TCP
recsys-bst-triton-predictor             ClusterIP   80/TCP,9000/TCP
recsys-bst-triton-candidate-predictor   ClusterIP   80/TCP,9000/TCP

deployment.apps/recsys-bst-triton-predictor             1/1
deployment.apps/recsys-bst-triton-candidate-predictor   1/1
pod/recsys-bst-triton-predictor-bb4c58c46-8mmbp              2/2 Running
pod/recsys-bst-triton-candidate-predictor-575544cb77-6xzkx   2/2 Running
```

### Image proof 

![Data & ML system](../../pngs/infer_service.png)

## Traffic Split Test

Run this from the workstation. It executes inside the API pod and calls the local API server, so it tests the real A/B router without depending on the public gateway or Docker local.

```bash
kubectl -n api-serving exec deploy/recsys-api-serving -c api -- \
  python -c 'import json, urllib.request, collections
counts=collections.Counter()
statuses=collections.Counter()
for user_id in range(1,81):
    payload={"user_id":user_id,"candidate_item_ids":[1,2,3,4,5,6,7,8,9,10],"top_k":5}
    req=urllib.request.Request("http://127.0.0.1:8080/recommendations", data=json.dumps(payload).encode(), headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            body=json.loads(res.read().decode())
            counts[body.get("ab_variant") or "none"] += 1
            statuses[str(res.status)] += 1
    except Exception as exc:
        statuses[type(exc).__name__] += 1
print("statuses", dict(statuses))
print("variants", dict(counts))'
```

Observed result:

```text
statuses {'200': 80}
variants {'control': 57, 'candidate': 23}
```

The result is close to the configured 20% candidate split. The exact count is deterministic for this user-id range because the router hashes `bst-stable-vs-candidate-20260630:<user_id>`.

### Image proof to capture:

![Data & ML system](../../pngs/api_serve_exec_ab.png)


## Prometheus Proof

Raw API metrics:

```bash
kubectl -n api-serving exec deploy/recsys-api-serving -c api -- \
  python -c 'import urllib.request
text=urllib.request.urlopen("http://127.0.0.1:8080/metrics", timeout=5).read().decode()
for line in text.splitlines():
    if "model_predictions_total" in line or "recsys_api_ab_assignments_total" in line:
        print(line)'
```

Observed result:

```text
model_predictions_total{ab_variant="candidate",experiment_id="bst-stable-vs-candidate-20260630",model_version="candidate-001",status="success"} 23.0
model_predictions_total{ab_variant="control",experiment_id="bst-stable-vs-candidate-20260630",model_version="stable-001",status="success"} 57.0
recsys_api_ab_assignments_total{ab_variant="candidate",experiment_id="bst-stable-vs-candidate-20260630",model_version="candidate-001"} 23.0
recsys_api_ab_assignments_total{ab_variant="control",experiment_id="bst-stable-vs-candidate-20260630",model_version="stable-001"} 57.0
```
### Image proof 

![Data & ML system](../../pngs/ab_prometheus_proof.png)

Prometheus table query:

```bash
kubectl -n observability exec deploy/recsys-prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/query?query=sum%28model_predictions_total%7Bexperiment_id%3D%22bst-stable-vs-candidate-20260630%22%7D%29%20by%20%28ab_variant%2Cmodel_version%2Cstatus%29'
```

Observed data:

```text
candidate candidate-001 success 23
control   stable-001    success 57
```

Latency and confidence proxy metrics:

```text
mean latency candidate-001 = 0.0196s
mean latency stable-001    = 0.0177s
mean confidence candidate  = 1.0
mean confidence control    = 1.0
```

### Image proof 

![Data & ML system](../../pngs/table_query.png)

## Grafana Dashboard

Dashboard: `Model A/B Testing`

URL through gateway:

```text
http://grafana.recsys.local/d/recsys-model-ab-testing/model-a-b-testing
```

Panels prepared for screenshots:

| Panel | Purpose |
|---|---|
| Prediction Rate | Request rate for the selected experiment. |
| Candidate Share | Actual candidate share observed by Prometheus. |
| Error Delta | Candidate error rate minus control error rate. |
| P95 Latency Delta | Candidate p95 latency minus control p95 latency. |
| Prediction Rate by Variant, Version, Status | Main table/time series for A/B result. |
| Current Traffic Split | Pie chart of control vs candidate. |
| Router Assignment Count | Confirms deterministic router assignment counts. |
| Model P95 Latency | Per-variant latency from `model_prediction_latency_seconds_bucket`. |
| Confidence Average / Distribution | Proxy quality without groundtruth. |
| Triton Pod CPU / Memory / Restarts | Runtime health for both inference services. |

### Image proof 

![Data & ML system](../../pngs/ab_dashboard_1.png)

![Data & ML system](../../pngs/ab_dashboard_2.png)

![Data & ML system](../../pngs/ab_dashboard_3.png)

![Data & ML system](../../pngs/ab_dashboard_4.png)