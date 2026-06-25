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

Source: `apps/api-serving/src/main.py`

Lines to show:

- `main.py:8`: imports `FastAPI`.
- `main.py:22`: creates the FastAPI app.
- `main.py:71-75`: initializes the Triton ranker/router from environment config.
- `main.py:121`: exposes the prediction endpoint `POST /recommendations`.
- `main.py:122-130`: runs the prediction flow and returns `RecommendationResponse`.

### Key Evidence

![Data & ML system](../../pngs/web-api-fast-api-demos.png)


## 2. Pydantic Validation

Source: `apps/api-serving/src/serving.py`

Lines to show:

- `serving.py:11`: imports `BaseModel` and `Field`.
- `serving.py:35-38`: validates `RecommendationRequest`.
- `serving.py:41-51`: defines prediction item and response schemas.

### Key Evidence

![Data & ML system](../../pngs/pydantic-web-api.png)


## 3. Async Prediction Function

Source: `apps/api-serving/src/main.py`

Lines to show:

- `main.py:122`: `async def recommendations(...)`.
- `main.py:124-130`: uses `await asyncio.to_thread(...)` so the FastAPI handler stays async while the blocking prediction flow runs in a worker thread.

### Key Evidence

![Data & ML system](../../pngs/fast-api-recommendation-code.png)

## 4. Model Prediction Flow

Source: `apps/api-serving/src/serving.py`

Lines to show:

- `serving.py:407`: `recommend(...)` starts the prediction flow.
- `serving.py:413`: selects the Triton route/model.
- `serving.py:419`: calls `_recommend_with_route(...)`.
- `serving.py:449-455`: pulls online features for the request user.
- `serving.py:469-471`: builds the Triton payload and scores it with `route.ranker.score(payload)`.
- `serving.py:475-486`: formats the top-k prediction response.

## 5. Triton Inference Engine

Source: `apps/api-serving/src/serving.py`

Lines to show:

- `serving.py:263`: `TritonRanker`.
- `serving.py:272-276`: creates `tritonclient.grpc.InferenceServerClient`.
- `serving.py:281-291`: builds Triton input/output tensors.
- `serving.py:293-296`: calls `client.infer(...)` and reads prediction scores.

![Data & ML system](../../pngs/triton-ranker.png)

## 6. Triton Runtime Config

Source: `infra/helm/recsys-serving/templates/api-configmap.yaml`

Lines to show:

- `api-configmap.yaml:7`: `TRITON_URL`.
- `api-configmap.yaml:8`: `TRITON_MODEL_NAME`.
- `api-configmap.yaml:12`: `MODEL_VERSION`.

#### Manual k8s curl command

```bash
kubectl run recsys-serving-e2e -n api-serving --rm -i --restart=Never \
  --image=curlimages/curl:8.10.1 -- \
  curl -fsS -X POST http://recsys-api-serving/recommendations \
  -H 'Content-Type: application/json' \
  -d '{"user_id":1,"top_k":5}'
```

## 8. Helm RollingUpdate + Healthcheck for K8s

Source: `infra/helm/recsys-serving/templates/api-deployment.yaml`

Lines to show:

- `api-deployment.yaml:12-18`: `RollingUpdate` strategy.
- `api-deployment.yaml:42-47`: startup probe on `/healthz`.
- `api-deployment.yaml:49-55`: readiness probe on `/ready`.
- `api-deployment.yaml:57-63`: liveness probe on `/healthz`.

### Evidence for Helm RollingUpdate + Healthcheck

![Data & ML system](../../pngs/healthcheck-k9s-helm.png)

## 9. Auto Fallback With Helm `--atomic`

#### Inside jenkins/scripts/model_cd.py line 195 & Jenkins file line 141

Note: fallback is applied to `api-serving` because `api-serving` is a resource inside the Helm release `recsys-serving`. This release is deployed with `helm upgrade --install --atomic`, so if the upgrade fails, Helm rolls back the entire release, including `recsys-api-serving`.

Source 1: `jenkins/scripts/model_cd.py`

Lines to show:

- `model_cd.py:195-211`: builds the Helm upgrade command.
- `model_cd.py:206`: includes `--atomic`.
- `model_cd.py:207-208`: includes the timeout.


![Data & ML system](../../pngs/atomic-auto-fall-back.png)

