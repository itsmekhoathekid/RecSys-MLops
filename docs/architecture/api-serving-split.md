# Independent Online Feature and Inference APIs

The serving boundary consists of three independently owned releases:

| Release | Namespace | Image | Responsibility |
|---|---|---|---|
| `recsys-online-feature-api` | `api-serving` | `recsys-online-feature-api` | Feast, Redis, PostgreSQL registry and online feature retrieval |
| `recsys-inference-api` | `api-serving` | `recsys-inference-api` | Feature HTTP client, ranking, Triton, A/B routing and shadow inference |
| `recsys-serving` | `kserve-triton-inference` | none | KServe runtimes, stable/candidate InferenceServices and Triton services |

The request path is:

```text
Gateway / Demo -> recsys-inference-api
              -> recsys-online-feature-api
              -> recsys-bst-triton(-candidate)
```

`recsys_serving_common.contracts` owns only the producer/consumer online-feature wire contract. Recommendation schemas remain private to Inference. Generic middleware, metrics, tracing and health/version helpers live in `recsys_serving_common`; Feast and Triton adapters never enter that package.

The Feast registry implementation is owned by `recsys-feature-store-runtime`. Data materialization, training and Online Feature API use the same package; no compatibility shim remains under `apps/data-platform/src/feature_store`.

## Deployment independence

Each API chart owns its Deployment, Service, configuration, ServiceMonitor and KEDA ScaledObject. The Inference release has no Helm dependency on the Feature release: compatibility is maintained through the versioned HTTP contract and expand/contract changes.

The image catalog contains 16 images. A Feature-only change builds/deploys only `recsys-online-feature-api`; an Inference-only change builds/deploys only `recsys-inference-api`; a shared serving change selects both. KServe-only changes build no API image.

Model CD writes `recsys-kserve-values.json` and `recsys-inference-api-values.json`. Mutating stages acquire the KServe lock before the Inference lock. Candidate readiness precedes routing changes; rollback restores champion-only Inference routing before candidate removal.

Metric names remain under `recsys_api_*` for dashboard continuity. The Inference `service` label and `OTEL_SERVICE_NAME` are `recsys-inference-api`; the Feature label remains `recsys-online-feature-api`. Readiness is process-local and does not cascade downstream availability.
