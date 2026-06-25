# Web API Pull Data

This note captures only the source-code evidence for the Web API requirement:

- FastAPI service.
- Pydantic request/response validation.
- Async API handlers.
- Helm deployment with `RollingUpdate`.
- Helm auto fallback through `--atomic`.
- CLI commands to verify the evidence.

## 1. FastAPI

Source: `apps/api-serving/src/main.py`

Lines to show:

- `main.py:8`: imports `FastAPI`.
- `main.py:22`: creates the app with `app = FastAPI(...)`.
- `main.py:103`: exposes the online feature pull endpoint.
- `main.py:121`: exposes the recommendation endpoint that sends features to inference.



### Key Evidence

![Data & ML system](../../pngs/web-api-fast-api-demos.png)

## 2. Pydantic Validation

Source: `apps/api-serving/src/serving.py`

Lines to show:

- `serving.py:11`: imports `BaseModel` and `Field`.
- `serving.py:35-38`: validates `RecommendationRequest`.
- `serving.py:46-58`: defines response schemas.

### Key code evidence:

![Data & ML system](../../pngs/pydantic-web-api.png)

## 3. Async API Functions

Source: `apps/api-serving/src/main.py`

Lines to show:

- `main.py:30`: async middleware.
- `main.py:79`: async health endpoint.
- `main.py:84`: async readiness endpoint.
- `main.py:99`: async metrics endpoint.
- `main.py:104`: async online feature pull endpoint.
- `main.py:122`: async recommendation endpoint.
- `main.py:110-116`: uses `await asyncio.to_thread(...)` for feature retrieval.
- `main.py:124-130`: uses `await asyncio.to_thread(...)` for recommendation/inference flow.

### Key code evidence:

![Data & ML system](../../pngs/fast-api-pull-data-code.png)

## 4. Pull Data From Online Feature Store

Source: `apps/api-serving/src/serving.py`

Lines to show:

- `serving.py:203`: `FeatureClient`.
- `serving.py:210-214`: connects to Redis online store.
- `serving.py:216-229`: pulls user sequence by `user_id`.
- `serving.py:231-244`: pulls item features by `item_id`.
- `serving.py:246-260`: pulls candidate item ids.
- `serving.py:489-502`: builds `OnlineFeaturesResponse`.

### Key Evidence

![Data & ML system](../../pngs/web-api-fast-api-docs.png)

## 6. Helm RollingUpdate + Healthcheck for K8s 

### Evidence for Helm RollingUpdate + Healthcheck


#### Run this command
```
kubectl describe deployment recsys-api-serving -n api-serving
```

![Data & ML system](../../pngs/healthcheck-k9s-helm.png)

#### Inside jenkins/scripts/model_cd.py line 195 & Jenkins file line 141

Note: fallback is applied to `api-serving` because `api-serving` is a resource inside the Helm release `recsys-serving`. This release is deployed with `helm upgrade --install --atomic`, so if the upgrade fails, Helm rolls back the entire release, including `recsys-api-serving`.

![Data & ML system](../../pngs/atomic-auto-fall-back.png)






