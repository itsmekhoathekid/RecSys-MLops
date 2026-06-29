# Web API Pull Data

This note captures only the source-code evidence for the Web API requirement:

- FastAPI service.
- Pydantic request/response validation.
- Async API handlers.
- Helm deployment with `RollingUpdate`.
- Helm auto fallback through `--atomic`.
- CLI commands to verify the evidence.

## 1. FastAPI

Source: [apps/api-serving/src/main.py line 1](../../../apps/api-serving/src/main.py#1)

Lines to show:

- [apps/api-serving/src/main.py line 8](../../../apps/api-serving/src/main.py#8): imports `FastAPI`.
- [apps/api-serving/src/main.py line 17](../../../apps/api-serving/src/main.py#17): creates the app with `app = FastAPI(...)`.
- [apps/api-serving/src/main.py line 98](../../../apps/api-serving/src/main.py#98): exposes the online feature pull endpoint.
- [apps/api-serving/src/main.py line 116](../../../apps/api-serving/src/main.py#116): exposes the recommendation endpoint that sends features to inference.



### Key Evidence

![Data & ML system](../../pngs/web-api-fast-api-demos.png)

## 2. Pydantic Validation

Source: [apps/api-serving/src/api_schemas.py line 1](../../../apps/api-serving/src/api_schemas.py#1)

Lines to show:

- [apps/api-serving/src/api_schemas.py line 5](../../../apps/api-serving/src/api_schemas.py#5): imports `BaseModel` and `Field`.
- [apps/api-serving/src/api_schemas.py line 8](../../../apps/api-serving/src/api_schemas.py#8): validates `RecommendationRequest`.
- [apps/api-serving/src/api_schemas.py line 14](../../../apps/api-serving/src/api_schemas.py#14): defines response schemas.

### Key code evidence:

![Data & ML system](../../pngs/pydantic-web-api.png)

## 3. Async API Functions

Source: [apps/api-serving/src/main.py line 1](../../../apps/api-serving/src/main.py#1)

Lines to show:

- [apps/api-serving/src/main.py line 24](../../../apps/api-serving/src/main.py#24): async middleware.
- [apps/api-serving/src/main.py line 73](../../../apps/api-serving/src/main.py#73): async health endpoint.
- [apps/api-serving/src/main.py line 78](../../../apps/api-serving/src/main.py#78): async readiness endpoint.
- [apps/api-serving/src/main.py line 93](../../../apps/api-serving/src/main.py#93): async metrics endpoint.
- [apps/api-serving/src/main.py line 98](../../../apps/api-serving/src/main.py#98): async online feature pull endpoint.
- [apps/api-serving/src/main.py line 116](../../../apps/api-serving/src/main.py#116): async recommendation endpoint.
- [apps/api-serving/src/main.py line 105-111](../../../apps/api-serving/src/main.py#105): uses `await asyncio.to_thread(...)` for feature retrieval.
- [apps/api-serving/src/main.py line 119-125](../../../apps/api-serving/src/main.py#119): uses `await asyncio.to_thread(...)` for recommendation/inference flow.

### Key code evidence:

![Data & ML system](../../pngs/fast-api-pull-data-code.png)

## 4. Pull Data From Online Feature Store

Source: [apps/api-serving/src/online_features.py line 1](../../../apps/api-serving/src/online_features.py#1)

Lines to show:

- [apps/api-serving/src/online_features.py line 22](../../../apps/api-serving/src/online_features.py#22): `FeatureClient`.
- [apps/api-serving/src/online_features.py line 29](../../../apps/api-serving/src/online_features.py#29): connects to Redis online store.
- [apps/api-serving/src/online_features.py line 35](../../../apps/api-serving/src/online_features.py#35): pulls user sequence by `user_id`.
- [apps/api-serving/src/online_features.py line 50](../../../apps/api-serving/src/online_features.py#50): pulls item features by `item_id`.
- [apps/api-serving/src/online_features.py line 65](../../../apps/api-serving/src/online_features.py#65): pulls candidate item ids.
- [apps/api-serving/src/online_features.py line 82](../../../apps/api-serving/src/online_features.py#82): builds `OnlineFeaturesResponse`.

### Key Evidence

![Data & ML system](../../pngs/web-api-fast-api-docs.png)

## 6. Helm RollingUpdate + Healthcheck for K8s 

### Evidence for Helm RollingUpdate + Healthcheck


#### Run this command
```
kubectl describe deployment recsys-api-serving -n api-serving
```

![Data & ML system](../../pngs/healthcheck-k9s-helm.png)

#### Inside [jenkins/scripts/model_cd.py line 207](../../../jenkins/scripts/model_cd.py#207) & [Jenkinsfile line 141](../../../Jenkinsfile#141)

Note: fallback is applied to `api-serving` because `api-serving` is a resource inside the Helm release `recsys-serving`. This release is deployed with `helm upgrade --install --atomic`, so if the upgrade fails, Helm rolls back the entire release, including `recsys-api-serving`.

Key lines:

- [jenkins/scripts/model_cd.py line 207](../../../jenkins/scripts/model_cd.py#207): builds the Helm deploy command.
- [jenkins/scripts/model_cd.py line 228](../../../jenkins/scripts/model_cd.py#228): enables `--atomic`.
- [Jenkinsfile line 141](../../../Jenkinsfile#141): runs deploy only when changed components should deploy.

![Data & ML system](../../pngs/atomic-auto-fall-back.png)


