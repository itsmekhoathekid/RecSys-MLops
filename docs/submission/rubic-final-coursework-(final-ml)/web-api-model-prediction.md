# Web API Model Prediction

This note captures the source-code and runtime evidence for the rubric item:

- Web API receives recommendation requests.
- The API uses FastAPI.
- Request and response schemas use Pydantic validation.
- API handlers are async.
- The API pulls online features from the feature API before prediction.
- The API sends the model payload to Triton Inference Server.
- The service exposes Kubernetes health checks.
- The service is deployed to Kubernetes with Helm `RollingUpdate`.
- Failed rollout fallback is handled by Helm `--atomic` at the `recsys-serving` release level.

## 1. Runtime Design

The deployed service for this rubric item is `recsys-api-serving`.

```text
Client
  -> recsys-api-serving POST /recommendations
  -> recsys-online-feature-api POST /online-features
  -> Feast SDK + Redis online store
  -> recsys-api-serving builds Triton tensors
  -> KServe/Triton gRPC inference
  -> RecommendationResponse
```

The prediction API does not read Redis directly in the split-serving path. It delegates online feature retrieval to `recsys-online-feature-api`, then converts the returned online features into Triton input tensors and formats the ranked response.

## 2. FastAPI Service

Code reference: [inference_api.py (line 18)](../../../apps/api-serving/src/inference_api.py#L18), [inference_api.py (line 123)](../../../apps/api-serving/src/inference_api.py#L123) configures the FastAPI app and exposes health, readiness, metrics, version, recommendation, and shutdown handlers.

### Key Evidence

![Recommendation API FastAPI proof](../../pngs/web-api-model-prediction-fastapi.png)

## 3. Pydantic Validation

Code reference: [api_schemas.py (line 8)](../../../apps/api-serving/src/api_schemas.py#L8), [api_schemas.py (line 37)](../../../apps/api-serving/src/api_schemas.py#L37) defines recommendation and online-feature request/response models and validation bounds.

### Key Evidence

![Pydantic web API proof](../../pngs/pydantic_pull_data_api.png)

## 4. Async API Functions

- [inference_api.py (line 75)](../../../apps/api-serving/src/inference_api.py#L75), [inference_api.py (line 119)](../../../apps/api-serving/src/inference_api.py#L119): async recommendation endpoint and awaited feature retrieval.
- [feature_service_client.py (line 12)](../../../apps/api-serving/src/feature_service_client.py#L12), [feature_service_client.py (line 34)](../../../apps/api-serving/src/feature_service_client.py#L34): `httpx.AsyncClient` POST to `/online-features` with Pydantic response validation.

### Key Evidence

![Recommendation API async proof](../../pngs/fast-api-model-prediction-code.png)

## 5. Pull Online Features Before Prediction

Code references: [inference_api.py (line 75)](../../../apps/api-serving/src/inference_api.py#L75), [inference_api.py (line 92)](../../../apps/api-serving/src/inference_api.py#L92) builds `OnlineFeaturesRequest` before prediction; [feature_service_client.py (line 12)](../../../apps/api-serving/src/feature_service_client.py#L12), [feature_service_client.py (line 34)](../../../apps/api-serving/src/feature_service_client.py#L34) performs and validates the service call.

### Key Evidence

![Recommendation API pulls online features proof](../../pngs/web_api_model_prediction_feature_pull.png)

## 6. Build Triton Payload And Predict

- [ranking.py (line 53)](../../../apps/api-serving/src/ranking.py#L53), [ranking.py (line 119)](../../../apps/api-serving/src/ranking.py#L119), [ranking.py (line 179)](../../../apps/api-serving/src/ranking.py#L179), [ranking.py (line 219)](../../../apps/api-serving/src/ranking.py#L219): normalizes online features, builds Triton tensors, invokes the selected route, and formats Top-K output.
- [triton.py (line 13)](../../../apps/api-serving/src/triton.py#L13), [triton.py (line 52)](../../../apps/api-serving/src/triton.py#L52): `RankerProtocol` and gRPC-backed `TritonRanker.score()`.

### Key Evidence

![Recommendation API to Triton proof](../../pngs/build_triton_payload.png)

## 7. KServe/Triton Inference Engine

Code reference: [inferenceservice.yaml (line 1)](../../../infra/helm/recsys-serving/templates/inferenceservice.yaml#L1), [inferenceservice.yaml (line 85)](../../../infra/helm/recsys-serving/templates/inferenceservice.yaml#L85) renders stable and optional candidate KServe `InferenceService` resources with Triton V2 and model `storageUri`.

Runtime command:

```bash
kubectl -n kserve-triton-inference get inferenceservice
kubectl -n kserve-triton-inference get pods
kubectl -n kserve-triton-inference get svc
```

### Image Proof

![Triton inference service proof](../../pngs/triton_get_pod.png)

## 8. A/B Route Support

Code reference: [ab_testing.py (line 13)](../../../apps/api-serving/src/ab_testing.py#L13), [ab_testing.py (line 151)](../../../apps/api-serving/src/ab_testing.py#L151) defines `TritonRoute`, environment-driven `TritonABRouter`, stable user assignment, shadow support, and route selection.

Runtime command:

```bash
kubectl -n api-serving exec deploy/recsys-api-serving -c api -- \
  python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8080/version", timeout=10).read().decode())'
```

### Image Proof

![Recommendation API A/B proof](../../pngs/ab_testing_api_prediction.png)

## 9. Runtime Verification Commands

Run these commands after `make gcp-services-up`.

```bash
kubectl -n api-serving get deploy,svc recsys-api-serving
kubectl -n api-serving rollout status deployment/recsys-api-serving --timeout=180s
kubectl -n api-serving rollout status deployment/recsys-online-feature-api --timeout=180s
kubectl -n kserve-triton-inference get inferenceservice,pods,svc
```

Healthcheck:

```bash
kubectl -n api-serving exec deploy/recsys-api-serving -c api -- \
  python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=10).read().decode()); print(urllib.request.urlopen("http://127.0.0.1:8080/ready", timeout=10).read().decode())'
```

End-to-end model prediction:

```bash
kubectl -n api-serving exec deploy/recsys-api-serving -c api -- \
  python -c 'import json, urllib.request; req=urllib.request.Request("http://127.0.0.1:8080/recommendations", data=json.dumps({"user_id":4,"candidate_item_ids":[1,2,3],"top_k":3}).encode(), headers={"Content-Type":"application/json"}, method="POST"); print(urllib.request.urlopen(req, timeout=30).read().decode())'
```

Expected recommendation output shape:

```json
{
  "user_id": 4,
  "model_version": "run_trial_ea87a_...",
  "ab_variant": "candidate",
  "ab_experiment_id": "bst-gcp-ab-20260701",
  "items": [
    {"item_id": 1, "score": 1.0000100135803223},
    {"item_id": 2, "score": 0.6666866540908813},
    {"item_id": 3, "score": 0.3333633244037628}
  ]
}
```

### Image Proof

![Recommendation API E2E proof](../../pngs/infer_gcp_exec.png)

## 10. Helm RollingUpdate + Healthcheck For K8s

Code reference: [api-deployment.yaml (line 9)](../../../infra/helm/recsys-serving/templates/api-deployment.yaml#L9), [api-deployment.yaml (line 84)](../../../infra/helm/recsys-serving/templates/api-deployment.yaml#L84) defines replicas, `RollingUpdate`, surge/unavailable limits, metrics annotations, and startup/readiness/liveness probes.

Runtime command:

```bash
kubectl -n api-serving describe deployment recsys-api-serving
```

Fields to capture:

| Capability | Expected evidence |
| --- | --- |
| Rolling update | `StrategyType: RollingUpdate` |
| No unavailable replicas during rollout | `Max Unavailable: 0` |
| Extra surge pod during rollout | `Max Surge: 1` |
| Startup probe | `http-get http://:http/healthz` |
| Readiness probe | `http-get http://:http/ready` |
| Liveness probe | `http-get http://:http/healthz` |

### Image Proof

![Recommendation API rolling update proof](../../pngs/healthcheck-k9s-helm.png)

![Recommendation API rolling update proof](../../pngs/health_k9s_infer_api.png)

## 11. Helm Auto Fallback With `--atomic`

The prediction API does not have a standalone Helm release. It is deployed as a resource inside the `recsys-serving` Helm release. Therefore, auto fallback for `recsys-api-serving` is inherited from the release-level `helm upgrade --install --atomic` command used by CI/CD. If the recommendation API rollout fails, Helm rolls back the whole `recsys-serving` release, including `recsys-api-serving`, `recsys-online-feature-api`, and the related serving resources.

Code reference: [model_cd.py (line 307)](../../../jenkins/scripts/model_cd.py#L307), [model_cd.py (line 404)](../../../jenkins/scripts/model_cd.py#L404) lints the chart and executes `helm upgrade --install --atomic` for the `recsys-serving` release.

Runtime command:

```bash
helm history recsys-serving -n kserve-triton-inference
helm status recsys-serving -n kserve-triton-inference
```

### Image Proof

![Helm atomic fallback proof](../../pngs/atomic-auto-fall-back.png)
