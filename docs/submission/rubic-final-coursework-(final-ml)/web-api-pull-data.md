# Web API Pull Data

This note captures the source-code and runtime evidence for the rubric item:

- Web API pulls data from the Online Feature Store by `user_id` and optional `candidate_item_ids`.
- The API uses FastAPI.
- Request and response schemas use Pydantic validation.
- API handlers are async.
- The service exposes Kubernetes health checks.
- The service is deployed to Kubernetes with Helm `RollingUpdate`.
- Failed rollout fallback is handled by Helm `--atomic`.

## 1. Runtime Design

The deployed online-feature service for this rubric item is `recsys-online-feature-api`. Its pull-data runtime is split into the following traceable parts.

```text
Client or recsys-api-serving
  -> [1] recsys-online-feature-api POST /online-features
  -> [2] request candidates or candidate:user:{user_id}
  -> [3] candidate:popular:global fallback
  -> [4] Redis realtime user features + Feast fallback
  -> [5] Feast batch item-feature lookup from Redis online store
  -> [6] OnlineFeaturesResponse
```

### 1.1 Request Entry Point And Validation

FastAPI exposes async POST and GET entry points. Pydantic validates `user_id`, optional `candidate_item_ids`, and `top_k`; the synchronous feature-store work is moved to a worker thread.

Code reference: [feature_api.py (line 55)](../../../apps/api-serving/src/feature_api.py#L55), [feature_api.py (line 69)](../../../apps/api-serving/src/feature_api.py#L69), [api_schemas.py (line 27)](../../../apps/api-serving/src/api_schemas.py#L27), [api_schemas.py (line 34)](../../../apps/api-serving/src/api_schemas.py#L34).

### 1.2 Personalized Candidate Resolution

Explicit `candidate_item_ids` take precedence. Otherwise, the service reads `candidate:user:{user_id}`, de-duplicates the result, and fills unoccupied slots from `candidate:popular:global` up to `max(top_k * 5, top_k)`.

Code reference: [online_features.py (line 238)](../../../apps/api-serving/src/online_features.py#L238), [online_features.py (line 273)](../../../apps/api-serving/src/online_features.py#L273).

### 1.3 Candidate-Pool Production

The Flink realtime job maintains global and category candidate sets. After a user interacts with a category, it merges that category's popular products into `candidate:user:{user_id}`, caps the pool at 100 products, and applies a seven-day TTL.

Code reference: [candidate_pool.py](../../../apps/data-platform/src/features/flink/features/candidate_pool.py), [redis_async.py](../../../apps/data-platform/src/features/flink/sinks/redis_async.py).

### 1.4 Realtime User-Feature Lookup

The service first reads `fs:user_sequence:{user_id}` and `fs:user_aggregate:{user_id}` directly from Redis. If no realtime sequence exists, it uses Feast `FeatureStore.get_online_features(...)` with the same `user_id` entity.

Code reference: [online_features.py (line 164)](../../../apps/api-serving/src/online_features.py#L164), [online_features.py (line 177)](../../../apps/api-serving/src/online_features.py#L177), [online_features.py (line 181)](../../../apps/api-serving/src/online_features.py#L181).

### 1.5 Batch Item-Feature Lookup

For the resolved product IDs, the service sends `product_id` entity rows to Feast in one batch. Feast resolves the configured item feature references from its Redis online store.

Code reference: [online_features.py (line 35)](../../../apps/api-serving/src/online_features.py#L35), [online_features.py (line 212)](../../../apps/api-serving/src/online_features.py#L212).

### 1.6 Online-Feature Response Assembly

The service returns the resolved candidate IDs, user sequence and aggregate features, and product feature rows as a validated `OnlineFeaturesResponse`.

Code reference: [online_features.py (line 273)](../../../apps/api-serving/src/online_features.py#L273), [api_schemas.py (line 27)](../../../apps/api-serving/src/api_schemas.py#L27).

### 1.7 Downstream Inference Boundary

The recommendation API is a separate service. It calls `recsys-online-feature-api`, receives the online-feature payload, builds the model tensors, and sends them to Triton Inference Server.

```text
Client
  -> recsys-api-serving POST /recommendations
  -> recsys-online-feature-api POST /online-features
  -> Feast SDK + Redis online store
  -> Triton inference
  -> ranked recommendations
```

Code reference: [feature_service_client.py (line 17)](../../../apps/api-serving/src/feature_service_client.py#L17), [inference_api.py (line 75)](../../../apps/api-serving/src/inference_api.py#L75), [inference_api.py (line 85)](../../../apps/api-serving/src/inference_api.py#L85), [inference_api.py (line 92)](../../../apps/api-serving/src/inference_api.py#L92).

## 2. FastAPI Service

Code reference: [feature_api.py (line 13)](../../../apps/api-serving/src/feature_api.py#L13), [feature_api.py (line 77)](../../../apps/api-serving/src/feature_api.py#L77) configures the FastAPI app and exposes warmup, health, readiness, metrics, plus async POST/GET handlers.

### Key Evidence

![Online feature API FastAPI proof](../../pngs/web-api-pull_data.png)

## 3. Pydantic Validation

Code reference: [api_schemas.py (line 27)](../../../apps/api-serving/src/api_schemas.py#L27), [api_schemas.py (line 37)](../../../apps/api-serving/src/api_schemas.py#L37) defines `OnlineFeaturesRequest`/`OnlineFeaturesResponse` and validation bounds.

### Key Evidence

![Pydantic web API proof](../../pngs/pydantic_pull_data_api.png)

## 4. Async API Functions

- [feature_api.py (line 55)](../../../apps/api-serving/src/feature_api.py#L55), [feature_api.py (line 77)](../../../apps/api-serving/src/feature_api.py#L77): async endpoints and `asyncio.to_thread(...)` around synchronous Feast access.
- [feature_service_client.py (line 17)](../../../apps/api-serving/src/feature_service_client.py#L17), [feature_service_client.py (line 29)](../../../apps/api-serving/src/feature_service_client.py#L29), [inference_api.py (line 75)](../../../apps/api-serving/src/inference_api.py#L75), [inference_api.py (line 92)](../../../apps/api-serving/src/inference_api.py#L92): async `httpx` service call before recommendation inference.

### Key Evidence

![Async pull data API proof](../../pngs/fast-api-pull-data-code.png)

## 5. Pull Data From Online Feature Store

Code reference: [online_features.py (line 124)](../../../apps/api-serving/src/online_features.py#L124), [online_features.py (line 164)](../../../apps/api-serving/src/online_features.py#L164), [online_features.py (line 238)](../../../apps/api-serving/src/online_features.py#L238), [online_features.py (line 273)](../../../apps/api-serving/src/online_features.py#L273) contains `FeatureClient` and `get_online_features()`: it configures Feast/Redis, loads user/item features, resolves personalized candidates with a global fallback, and returns `OnlineFeaturesResponse`.

Feast store definition:

| Layer | Implementation | Runtime usage |
| --- | --- | --- |
| Offline store | PostgreSQL is the Feast core offline store. Spark exports lakehouse-derived batch feature tables into the Feast PostgreSQL schema, and Flink writes streaming feature rows into the same offline-store backend. | Used by training, validation, drift checks, and Feast historical retrieval/materialization. |
| Online store | Redis | Used by `recsys-online-feature-api` through Feast SDK `get_online_features(...)` during serving. |

### Key Evidence

![Online feature API docs proof](../../pngs/web-api-fast-api-docs.png)

## 6. Service Composition With Inference API

The rubric sentence says this Web API pulls data from the Online Feature Store and then sends data to an ML inference engine. In this implementation the responsibility is split into two services:

| Service | Responsibility |
| --- | --- |
| `recsys-online-feature-api` | Pulls user and item online features from Feast/Redis. |
| `recsys-api-serving` | Calls `recsys-online-feature-api`, prepares ranking features, and calls Triton inference. |

Code references:

- [inference_api.py (line 75)](../../../apps/api-serving/src/inference_api.py#L75), [inference_api.py (line 104)](../../../apps/api-serving/src/inference_api.py#L104): `recommendations()` fetches online features, selects a Triton route, and ranks candidates.
- [feature_service_client.py (line 17)](../../../apps/api-serving/src/feature_service_client.py#L17), [feature_service_client.py (line 29)](../../../apps/api-serving/src/feature_service_client.py#L29): async POST to `/online-features` plus Pydantic response validation.

## 7. Runtime Verification Commands

Run these commands after `make gcp-services-up`.

```bash
kubectl -n api-serving get deploy,svc recsys-online-feature-api
kubectl -n api-serving rollout status deployment/recsys-online-feature-api --timeout=180s
kubectl -n api-serving rollout status deployment/recsys-api-serving --timeout=180s
```

Healthcheck:

```bash
kubectl -n api-serving exec deploy/recsys-online-feature-api -c api -- \
  python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=10).read().decode()); print(urllib.request.urlopen("http://127.0.0.1:8080/ready", timeout=10).read().decode())'
```

Online feature pull:

```bash
kubectl -n api-serving exec deploy/recsys-online-feature-api -c api -- \
  python -c 'import json, urllib.request; req=urllib.request.Request("http://127.0.0.1:8080/online-features", data=json.dumps({"user_id":4,"candidate_item_ids":[1,2,3],"top_k":3}).encode(), headers={"Content-Type":"application/json"}, method="POST"); print(urllib.request.urlopen(req, timeout=20).read().decode())'
```

End-to-end pull-data plus inference:

```bash
kubectl -n api-serving exec deploy/recsys-api-serving -c api -- \
  python -c 'import json, urllib.request; req=urllib.request.Request("http://127.0.0.1:8080/recommendations", data=json.dumps({"user_id":4,"candidate_item_ids":[1,2,3],"top_k":3}).encode(), headers={"Content-Type":"application/json"}, method="POST"); print(urllib.request.urlopen(req, timeout=30).read().decode())'
```

Expected online feature output shape:

```json
{
  "user_id": 4,
  "candidate_item_ids": [1, 2, 3],
  "user_sequence": {
    "hist_item_ids": [104, 70],
    "hist_length": 14,
    "views_30m": 12
  },
  "item_features": {
    "1": {
      "category_id": 16,
      "brand_id": 31,
      "popularity_score": 4.0
    }
  }
}
```

### Image Proof

![Online feature API proof](../../pngs/k9s_api_pull_data.png)

![Online feature API proof](../../pngs/web_api_pull_online_features.png)

## 8. Helm RollingUpdate + Healthcheck For K8s

Code reference: [feature-api-deployment.yaml (line 11)](../../../infra/helm/recsys-serving/templates/feature-api-deployment.yaml#L11), [feature-api-deployment.yaml (line 83)](../../../infra/helm/recsys-serving/templates/feature-api-deployment.yaml#L83) defines replicas, `RollingUpdate`, surge/unavailable limits, metrics annotations, and startup/readiness/liveness probes.

Runtime command:

```bash
kubectl -n api-serving describe deployment recsys-online-feature-api
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

![Online feature API rolling update proof](../../pngs/web_api_pull_data_rolling_update.png)

## 9. Helm Auto Fallback With `--atomic`

Auto fallback is handled at the Helm release level. The service is part of the `recsys-serving` release. When CI/CD deploys this release with `helm upgrade --install --atomic`, Helm automatically rolls the release back if the new rollout fails.

Code reference: [model_cd.py (line 333)](../../../jenkins/scripts/model_cd.py#L333) lints the chart; [line 339](../../../jenkins/scripts/model_cd.py#L339) enables atomic deployment by default; [line 340](../../../jenkins/scripts/model_cd.py#L340), [line 357](../../../jenkins/scripts/model_cd.py#L357), and [line 403](../../../jenkins/scripts/model_cd.py#L403) build and execute both `recsys-serving` Helm upgrades.

Runtime command:

```bash
helm history recsys-serving -n kserve-triton-inference
helm status recsys-serving -n kserve-triton-inference
```

### Image Proof

![Helm atomic fallback proof](../../pngs/atomic-auto-fall-back.png)
