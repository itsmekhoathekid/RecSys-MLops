# Web API Model Prediction

This note captures only the source-code evidence for the Web API model prediction requirement:

- FastAPI prediction endpoint.
- Pydantic request/response validation.
- Async API handler.
- Online features sent to the ML inference engine.
- Triton model prediction call.
- Helm deployment with `RollingUpdate`, health checks, and auto fallback through `--atomic`.
- CLI commands to verify the evidence.

## 1. FastAPI Prediction Endpoint

Source: [apps/api-serving/src/main.py line 1](../../../apps/api-serving/src/main.py#1)

Lines to show:

- [apps/api-serving/src/main.py line 8](../../../apps/api-serving/src/main.py#8): imports `FastAPI`.
- [apps/api-serving/src/main.py line 17](../../../apps/api-serving/src/main.py#17): creates the FastAPI app.
- [apps/api-serving/src/main.py line 66](../../../apps/api-serving/src/main.py#66): initializes the Triton ranker/router from environment config.
- [apps/api-serving/src/main.py line 116](../../../apps/api-serving/src/main.py#116): exposes the prediction endpoint `POST /recommendations`.
- [apps/api-serving/src/main.py line 119](../../../apps/api-serving/src/main.py#119): runs the prediction flow and returns `RecommendationResponse`.

### Key Evidence

![Data & ML system](../../pngs/web-api-fast-api-demos.png)


## 2. Pydantic Validation

Source: [apps/api-serving/src/api_schemas.py line 1](../../../apps/api-serving/src/api_schemas.py#1)

Lines to show:

- [apps/api-serving/src/api_schemas.py line 5](../../../apps/api-serving/src/api_schemas.py#5): imports `BaseModel` and `Field`.
- [apps/api-serving/src/api_schemas.py line 8](../../../apps/api-serving/src/api_schemas.py#8): validates `RecommendationRequest`.
- [apps/api-serving/src/api_schemas.py line 14](../../../apps/api-serving/src/api_schemas.py#14): defines prediction item and response schemas.

### Key Evidence

![Data & ML system](../../pngs/pydantic-web-api.png)


## 3. Async Prediction Function

Source: [apps/api-serving/src/main.py line 1](../../../apps/api-serving/src/main.py#1)

Lines to show:

- [apps/api-serving/src/main.py line 116](../../../apps/api-serving/src/main.py#116): `async def recommendations(...)`.
- [apps/api-serving/src/main.py line 119-125](../../../apps/api-serving/src/main.py#119): uses `await asyncio.to_thread(...)` so the FastAPI handler stays async while the blocking prediction flow runs in a worker thread.

### Key Evidence

![Data & ML system](../../pngs/fast-api-recommendation-code.png)

## 4. Model Prediction Flow

Source: [apps/api-serving/src/ranking.py line 1](../../../apps/api-serving/src/ranking.py#1)

Lines to show:

- [apps/api-serving/src/ranking.py line 122](../../../apps/api-serving/src/ranking.py#122): `recommend(...)` starts the prediction flow.
- [apps/api-serving/src/ranking.py line 128](../../../apps/api-serving/src/ranking.py#128): selects the Triton route/model.
- [apps/api-serving/src/ranking.py line 134](../../../apps/api-serving/src/ranking.py#134): calls `_recommend_with_route(...)`.
- [apps/api-serving/src/ranking.py line 164](../../../apps/api-serving/src/ranking.py#164): pulls online features for the request user.
- [apps/api-serving/src/ranking.py line 184](../../../apps/api-serving/src/ranking.py#184): builds the Triton payload and scores it with `route.ranker.score(payload)`.
- [apps/api-serving/src/ranking.py line 190](../../../apps/api-serving/src/ranking.py#190): formats the top-k prediction response.

## 5. Triton Inference Engine

Source: [apps/api-serving/src/triton.py line 1](../../../apps/api-serving/src/triton.py#1)

Lines to show:

- [apps/api-serving/src/triton.py line 18](../../../apps/api-serving/src/triton.py#18): `TritonRanker`.
- [apps/api-serving/src/triton.py line 27](../../../apps/api-serving/src/triton.py#27): creates `tritonclient.grpc.InferenceServerClient`.
- [apps/api-serving/src/triton.py line 35](../../../apps/api-serving/src/triton.py#35): builds Triton input/output tensors.
- [apps/api-serving/src/triton.py line 46](../../../apps/api-serving/src/triton.py#46): calls `client.infer(...)` and reads prediction scores.

![Data & ML system](../../pngs/triton-ranker.png)

## 6. Triton Runtime Config

Source: [infra/helm/recsys-serving/templates/api-configmap.yaml line 1](../../../infra/helm/recsys-serving/templates/api-configmap.yaml#1)

Lines to show:

- [infra/helm/recsys-serving/templates/api-configmap.yaml line 7](../../../infra/helm/recsys-serving/templates/api-configmap.yaml#7): `TRITON_URL`.
- [infra/helm/recsys-serving/templates/api-configmap.yaml line 8](../../../infra/helm/recsys-serving/templates/api-configmap.yaml#8): `TRITON_MODEL_NAME`.
- [infra/helm/recsys-serving/templates/api-configmap.yaml line 12](../../../infra/helm/recsys-serving/templates/api-configmap.yaml#12): `MODEL_VERSION`.

#### Manual k8s curl command

```bash
kubectl run recsys-serving-e2e -n api-serving --rm -i --restart=Never \
  --image=curlimages/curl:8.10.1 -- \
  curl -fsS -X POST http://recsys-api-serving/recommendations \
  -H 'Content-Type: application/json' \
  -d '{"user_id":1,"candidate_item_ids":[1,2,3,4,5,6,7,8,9,10],"top_k":5}'
```

## 8. Helm RollingUpdate + Healthcheck for K8s

Source: [infra/helm/recsys-serving/templates/api-deployment.yaml line 1](../../../infra/helm/recsys-serving/templates/api-deployment.yaml#1)

Lines to show:

- [infra/helm/recsys-serving/templates/api-deployment.yaml line 12-18](../../../infra/helm/recsys-serving/templates/api-deployment.yaml#12): `RollingUpdate` strategy.
- [infra/helm/recsys-serving/templates/api-deployment.yaml line 42-47](../../../infra/helm/recsys-serving/templates/api-deployment.yaml#42): startup probe on `/healthz`.
- [infra/helm/recsys-serving/templates/api-deployment.yaml line 49-55](../../../infra/helm/recsys-serving/templates/api-deployment.yaml#49): readiness probe on `/ready`.
- [infra/helm/recsys-serving/templates/api-deployment.yaml line 57-63](../../../infra/helm/recsys-serving/templates/api-deployment.yaml#57): liveness probe on `/healthz`.

### Evidence for Helm RollingUpdate + Healthcheck

![Data & ML system](../../pngs/healthcheck-k9s-helm.png)

## 9. Auto Fallback With Helm `--atomic`

#### Inside [jenkins/scripts/model_cd.py line 207](../../../jenkins/scripts/model_cd.py#207) & [Jenkinsfile line 141](../../../Jenkinsfile#141)

Note: fallback is applied to `api-serving` because `api-serving` is a resource inside the Helm release `recsys-serving`. This release is deployed with `helm upgrade --install --atomic`, so if the upgrade fails, Helm rolls back the entire release, including `recsys-api-serving`.

Source 1: [jenkins/scripts/model_cd.py line 207](../../../jenkins/scripts/model_cd.py#207)

Lines to show:

- [jenkins/scripts/model_cd.py line 207-230](../../../jenkins/scripts/model_cd.py#207): builds the Helm upgrade command.
- [jenkins/scripts/model_cd.py line 228](../../../jenkins/scripts/model_cd.py#228): includes `--atomic`.
- [jenkins/scripts/model_cd.py line 223-226](../../../jenkins/scripts/model_cd.py#223): includes the timeout.
- [Jenkinsfile line 141](../../../Jenkinsfile#141): runs deploy only when changed components should deploy.


![Data & ML system](../../pngs/atomic-auto-fall-back.png)
