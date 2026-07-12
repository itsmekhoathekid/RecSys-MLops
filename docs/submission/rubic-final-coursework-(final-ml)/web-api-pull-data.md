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

The deployed service for this rubric item is `recsys-online-feature-api`.

```text
Client or recsys-api-serving
  -> recsys-online-feature-api POST /online-features
  -> Feast SDK FeatureStore.get_online_features(...)
  -> Redis online store in recsys-dataflow
  -> OnlineFeaturesResponse
```

The recommendation API is a separate service. It calls `recsys-online-feature-api`, receives the online feature payload, and then sends the model tensor payload to Triton Inference Server.

```text
Client
  -> recsys-api-serving POST /recommendations
  -> recsys-online-feature-api POST /online-features
  -> Feast SDK + Redis online store
  -> Triton inference
  -> ranked recommendations
```

## 2. FastAPI Service

Code reference: [`feature_api.py`](../../../apps/api-serving/src/feature_api.py) configures the FastAPI app and exposes health, readiness, metrics, plus async POST/GET online-feature handlers.

### Key Evidence

![Online feature API FastAPI proof](../../pngs/web-api-pull_data.png)

## 3. Pydantic Validation

Code reference: [`api_schemas.py`](../../../apps/api-serving/src/api_schemas.py) defines `OnlineFeaturesRequest`/`OnlineFeaturesResponse` and validates `user_id`, candidate-list length, and `top_k`.

### Key Evidence

![Pydantic web API proof](../../pngs/pydantic_pull_data_api.png)

## 4. Async API Functions

- [`feature_api.py`](../../../apps/api-serving/src/feature_api.py): async endpoints and `asyncio.to_thread(...)` around synchronous Feast access.
- [`feature_service_client.py`](../../../apps/api-serving/src/feature_service_client.py), [`inference_api.py`](../../../apps/api-serving/src/inference_api.py): async `httpx` service call before recommendation inference.

### Key Evidence

![Async pull data API proof](../../pngs/fast-api-pull-data-code.png)

## 5. Pull Data From Online Feature Store

Code reference: [`online_features.py`](../../../apps/api-serving/src/online_features.py) contains `FeatureClient` and `get_online_features()`: it configures Feast/Redis, loads user and item features, resolves candidate ids, and returns `OnlineFeaturesResponse`.

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

- [`inference_api.py`](../../../apps/api-serving/src/inference_api.py): `recommendations()` fetches online features, selects a Triton route, and ranks candidates.
- [`feature_service_client.py`](../../../apps/api-serving/src/feature_service_client.py): async POST to `/online-features` plus Pydantic response validation.

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

Code reference: [`feature-api-deployment.yaml`](../../../infra/helm/recsys-serving/templates/feature-api-deployment.yaml) defines replicas, `RollingUpdate`, surge/unavailable limits, and startup/readiness/liveness probes.

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

Code reference: [`model_cd.py`](../../../jenkins/scripts/model_cd.py) lints the chart and executes `helm upgrade --install --atomic` for the `recsys-serving` release.

Runtime command:

```bash
helm history recsys-serving -n kserve-triton-inference
helm status recsys-serving -n kserve-triton-inference
```

### Image Proof

![Helm atomic fallback proof](../../pngs/atomic-auto-fall-back.png)
