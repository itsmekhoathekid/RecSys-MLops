# Async API Serving Runtime

The FastAPI services keep their public HTTP, schema, A/B, fallback, and MCP
contracts unchanged while separating async-native I/O from blocking SDK work.
This document is the code-reference index for that runtime boundary.

## Concurrency Model

| Runtime path | Concurrency behavior | Implementation | Tests |
| --- | --- | --- | --- |
| Shared serving runtime | `AsyncCapacityLimiter` bounds native async calls. `BoundedExecutor` bounds workers plus queued calls and releases a slot only after the underlying blocking future finishes, including after caller cancellation. Saturation records in-flight, wait, and rejection metrics. | [`concurrency.py`](shared/src/recsys_serving_common/concurrency.py), [`observability.py`](shared/src/recsys_serving_common/observability.py) | [`test_concurrency.py`](../../tests/unit/api_serving/test_concurrency.py) |
| Inference API | Fetch online features asynchronously, build the Triton payload, submit optional shadow work, await primary inference, and format the existing response. Capacity exhaustion is returned as `503`. | [`app.py`](inference-api/src/recsys_inference_api/app.py), [`ranking.py`](inference-api/src/recsys_inference_api/ranking.py) | [`test_serving.py`](../../tests/unit/api_serving/test_serving.py), [`test_split_services.py`](../../tests/unit/api_serving/test_split_services.py) |
| Triton | `RankerProtocol.score()` and `TritonRanker.score()` are async and use `tritonclient.grpc.aio`. Primary and A/B rankers share one limiter. The client request and coroutine both use the configured timeout. | [`triton.py`](inference-api/src/recsys_inference_api/triton.py), [`ab_testing.py`](inference-api/src/recsys_inference_api/ab_testing.py) | [`test_serving.py`](../../tests/unit/api_serving/test_serving.py), [`test_split_services.py`](../../tests/unit/api_serving/test_split_services.py) |
| Shadow inference | Shadow tasks use async rankers, are bounded by pending and running limits, cancel timed-out inference, drain during shutdown, and do not release concurrency before the ranker coroutine ends. | [`shadow.py`](inference-api/src/recsys_inference_api/shadow.py), [inference lifespan](inference-api/src/recsys_inference_api/app.py) | [`test_serving.py`](../../tests/unit/api_serving/test_serving.py) |
| Online Feature API | `redis.asyncio.Redis` uses a bounded connection pool and `MGET` for realtime user sequence plus aggregates. Blocking Feast calls run in a one-worker bounded executor. User and item feature reads start concurrently after candidate selection. | [`service.py`](online-feature-api/src/recsys_online_feature_api/service.py), [`app.py`](online-feature-api/src/recsys_online_feature_api/app.py) | [`test_serving.py`](../../tests/unit/api_serving/test_serving.py), [`test_split_services.py`](../../tests/unit/api_serving/test_split_services.py) |
| RAG API | ONNX, Feast, Milvus, and boto3 operations run in a bounded request executor. Readiness uses a separate control executor, while MinIO and Milvus calls receive storage timeouts. | [`app.py`](rag-api/src/recsys_rag_api/app.py), [`retrieval.py`](rag-api/src/recsys_rag_api/retrieval.py), [`chunk_lookup.py`](rag-api/src/recsys_rag_api/chunk_lookup.py) | [`rag_api` tests](../../tests/unit/api_serving/rag_api/) |
| Feature/RAG MCP | Downstream HTTP stays async, uses explicit connection and keep-alive limits, and closes both clients in the FastAPI lifespan. | [`app.py`](../agentic/recsys-feature-rag-mcp/src/recsys_feature_rag_mcp/app.py), [`settings.py`](../agentic/recsys-feature-rag-mcp/src/recsys_feature_rag_mcp/settings.py) | [`feature_rag_mcp` tests](../../tests/unit/agentic/feature_rag_mcp/) |
| Demo backend | The repository uses Psycopg `AsyncConnectionPool`; queries, transactions, commit, rollback, and pool lifecycle are awaited. | [`database.py`](../demo-web/backend/app/database.py), [`main.py`](../demo-web/backend/app/main.py) | [`test_database.py`](../demo-web/backend/tests/test_database.py), [`test_demo_api.py`](../demo-web/backend/tests/test_demo_api.py) |

## Runtime Defaults And Helm Wiring

| Service | Defaults | Application settings | Helm source |
| --- | --- | --- | --- |
| Inference | Triton timeout `5s`, maximum concurrency `16`, capacity wait `0.05s` | [`settings.py`](inference-api/src/recsys_inference_api/settings.py) | [`configmap.yaml`](../../infra/helm/recsys-inference-api/templates/configmap.yaml), [`values.yaml`](../../infra/helm/recsys-inference-api/values.yaml) |
| Online Feature | Redis connections `32`, socket/connect timeout `1s`; Feast workers `1`, queue `15`, capacity wait `0.05s` | [`settings.py`](online-feature-api/src/recsys_online_feature_api/settings.py) | [`configmap.yaml`](../../infra/helm/recsys-online-feature-api/templates/configmap.yaml), [`values.yaml`](../../infra/helm/recsys-online-feature-api/values.yaml) |
| RAG | Sync workers `8`, queue `16`, capacity wait `0.1s`, storage timeout `5s` | [`settings.py`](rag-api/src/recsys_rag_api/settings.py) | [`configmap.yaml`](../../infra/helm/recsys-rag-api/templates/configmap.yaml), [`values.yaml`](../../infra/helm/recsys-rag-api/values.yaml) |
| MCP | Downstream connections `50`, keep-alive connections `20` | [`settings.py`](../agentic/recsys-feature-rag-mcp/src/recsys_feature_rag_mcp/settings.py) | [`configmap.yaml`](../../infra/helm/recsys-feature-rag-mcp/templates/configmap.yaml), [`values.yaml`](../../infra/helm/recsys-feature-rag-mcp/values.yaml) |
| Demo | PostgreSQL pool `1-8`, pool/connect timeout `5s` | [`database.py`](../demo-web/backend/app/database.py) | [`values.yaml`](../../infra/helm/recsys-demo-web/values.yaml) |

## CI/CD Traceability

The root Jenkins job is triggered by GitHub pushes. A push to a PR branch runs
the affected CI/build proof without publishing or deploying; merging a PR
creates a push on `main`, where the same revision gate enables immutable image
publication and production deployment.

- Trigger and stage flow: [`Jenkinsfile`](../../Jenkinsfile).
- Main-revision publish/deploy gate: [`component_pipeline.groovy`](../../jenkins/pipeline/component_pipeline.groovy).
- Path ownership for the five affected components: [`components.json`](../../jenkins/config/components.json).
- Serving CI suites, including concurrency tests: [`serving.sh`](../../jenkins/scripts/ci/serving.sh), [`agentic.sh`](../../jenkins/scripts/ci/agentic.sh), and [`demo.sh`](../../jenkins/scripts/ci/demo.sh).
- Deployment and production verification: [`deploy/serving.sh`](../../jenkins/scripts/deploy/serving.sh), [`deploy/rag.sh`](../../jenkins/scripts/deploy/rag.sh), [`test/serving.sh`](../../jenkins/scripts/test/serving.sh), and [`test/rag.sh`](../../jenkins/scripts/test/rag.sh).

Documentation-only changes are excluded by change detection, so updating this
reference does not publish or redeploy serving images.
