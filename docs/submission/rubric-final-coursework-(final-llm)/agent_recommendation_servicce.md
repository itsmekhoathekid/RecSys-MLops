# Sandboxed Recommendation Agent Uses the Recommendation Service

> **Runtime status (updated 2026-08-28):** production runs the custom kagent v7
> compatibility image with Substrate `0.0.11`; values select assigned-worker
> KEDA. Recommendation proved `1 -> 2 -> 3 -> 2 -> 1`, completed `2187/2187`
> load requests, and proved fallback to one. Revision v9 copies `user_id`,
> `candidate_item_ids`, and `top_k` exactly from each request, then emits final
> text immediately after the single MCP response without calling `ask_user`.
> CPU mode remains only for rollback.

This submission section proves that the recommendation flow:

- exposes the existing `POST /recommendations` serving boundary through a
  dedicated FastAPI and FastMCP service;
- validates tool inputs and downstream outputs with Pydantic;
- uses asynchronous, pooled HTTP transport without importing Feast, Redis,
  Triton, or the context/RAG agent runtime;
- deploys the MCP server with Helm, zero-unavailable `RollingUpdate`, KEDA
  autoscaling, PDB protection, and scaler fallback;
- runs one declarative `SandboxAgent` profile on a dedicated gVisor
  `WorkerPool` scaled by KEDA from one to three workers;
- publishes both the MCP server and SandboxAgent to Agent Registry; and
- demonstrates a grounded recommendation call in the kagent Chat UI.

The online-feature implementation used internally by the inference service is
documented separately in
[Web API Pull Data](<../rubic-final-coursework-(final-ml)/web-api-pull-data.md>).
The context/RAG agent is documented in
[Agent Pull Data](agent_pull_data.md). The recommendation agent does **not**
call that agent: it makes one recommendation MCP call, and the existing
inference API owns feature retrieval, Triton routing, scoring, and Top-K.

## 1. Implemented Architecture

```text
kagent Chat UI
  -> SandboxAgent/recsys-recommendation-agent-sandbox
  -> WorkerPool/recsys-recommendation-sandbox-pool
       -> Substrate ateom-gvisor workers
       -> KEDA targets ate.dev/v1alpha1 WorkerPool /scale
  -> RemoteMCPServer/recsys-recommendation-mcp
  -> recsys-recommendation-mcp FastAPI + FastMCP Deployment
       -> POST recsys-inference-api.api-serving.svc/recommendations
            -> recsys-online-feature-api
            -> KServe/Triton BST
            -> model-ranked Top-K + A/B metadata

Prometheus
  <- recsys-kubernetes-pods scrapes annotated MCP and inference API pods
  -> KEDA Prometheus scalers
  -> HPA controls MCP Deployment and recommendation WorkerPool

Agent Registry
  <- Jenkins publishes versioned MCPServer and SandboxAgent metadata
     only after protocol, runtime, and A2A smoke tests pass
```

There is no `type: Agent`, A2A reference, context-agent tool, feature/RAG MCP
tool, or LLM reranking step in this flow. `SandboxAgent` is the single agent
profile. Multi-replica execution is supplied by its dedicated WorkerPool.

### 1.1 Shared autoscaling control loop

All three scalable runtime layers follow the same native Kubernetes loop:

```text
Application completes work or Substrate assigns a sandbox worker
  -> Prometheus application rate or assigned-worker gauge increases
  -> recsys-prometheus discovers annotated pods and scrapes /metrics
  -> KEDA queries Prometheus every polling interval
  -> KEDA external metric is consumed by an HPA
  -> HPA writes the target /scale subresource
  -> Deployment or WorkerPool controller reconciles replicas
  -> readiness gates make new replicas Available
```

The recommendation inference API, recommendation MCP, and sandbox WorkerPool
scale independently. Their production bounds are all `min=1`, `max=3`. The MCP
and WorkerPool additionally configure KEDA fallback to one replica.

### 1.2 Recommendation FastAPI autoscaling stages

```mermaid
flowchart LR
    Request[POST /recommendations] --> API[Inference API pod]
    API -->|request counter and duration histogram| Metrics[Pod :8080/metrics]
    Prometheus[recsys-prometheus] -->|scrape annotated pod every 15s| Metrics
    KEDA[KEDA Prometheus scaler] -->|rate and latency PromQL| Prometheus
    KEDA --> HPA[KEDA-generated HPA]
    HPA -->|Deployment /scale| Deployment[recsys-inference-api Deployment]
    Deployment --> Pods[1..3 Ready API pods]
```

#### Stage 1: completed recommendation requests produce the signal

The inference service owns the public request and emits the shared API request
counter after request completion:

```python
@app.post("/recommendations", response_model=RecommendationResponse)
async def recommendations(
    payload: RecommendationRequest,
    request: Request,
) -> RecommendationResponse:
    online_features = await request.app.state.feature_service.fetch(...)
    response = await recommend_from_online_features(
        online_features=online_features,
        top_k=payload.top_k,
        route=route,
    )
    return response
```

References:

- [Inference FastAPI endpoint (line 96)](../../../apps/api-serving/inference-api/src/recsys_inference_api/app.py#L96)
- [Shared serving request observability (line 235)](../../../apps/api-serving/shared/src/recsys_serving_common/observability.py#L235)

#### Stage 2: Prometheus scrapes request rate and latency

```yaml
# recsys-inference-api Deployment pod template
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/path: /metrics
  prometheus.io/port: "8080"

# recsys-prometheus scrape configuration
global:
  scrape_interval: 15s
scrape_configs:
  - job_name: recsys-kubernetes-pods
    kubernetes_sd_configs:
      - role: pod
        namespaces:
          names: [api-serving]
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
        action: keep
        regex: "true"
      - source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
        action: replace
        target_label: __address__
        regex: ([^:]+)(?::\d+)?;(\d+)
        replacement: $1:$2
```

The active production path uses the standalone `recsys-prometheus` Deployment,
not an Operator-managed Prometheus CR. It scrapes every matching pod endpoint
independently and stores the samples in its TSDB PVC.

The inference ScaledObject queries both completed `POST /recommendations`
request rate and average latency:

```yaml
query: >-
  sum(rate(recsys_api_requests_total{
    service="recsys-inference-api",
    route="/recommendations",
    method="POST"
  }[1m]))
```

References:

- [Inference pod scrape annotations](../../../infra/helm/recsys-inference-api/templates/deployment.yaml#L23)
- [standalone Prometheus pod-discovery job](../../../infra/helm/recsys-observability/templates/prometheus.yaml#L192)
- [Inference KEDA queries (line 18)](../../../infra/helm/recsys-inference-api/templates/scaledobject.yaml#L18)

#### Stage 3: KEDA and HPA select `1..3`

```yaml
autoscaling:
  enabled: true
  minReplicas: 1
  maxReplicas: 3
  pollingInterval: 10
  cooldownPeriod: 120
  requestRate:
    targetValue: "5"
  latency:
    targetValue: "0.20"
```

Reference: [Inference API autoscaling values (line 51)](../../../infra/helm/recsys-inference-api/values.yaml#L51).

#### Stage 4: RollingUpdate and probes complete scale-out

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 0
    maxSurge: 1
containers:
  - name: api
    startupProbe: {httpGet: {path: /healthz, port: http}}
    readinessProbe: {httpGet: {path: /ready, port: http}}
    livenessProbe: {httpGet: {path: /healthz, port: http}}
```

Reference: [Inference Deployment strategy and probes (line 13)](../../../infra/helm/recsys-inference-api/templates/deployment.yaml#L13).

### 1.3 Recommendation MCP autoscaling stages

```mermaid
flowchart LR
    Agent[Recommendation SandboxAgent] --> MCP[Recommendation MCP pod]
    MCP -->|increment tool-call counter| Metrics[Pod :8080/metrics]
    Prometheus[recsys-prometheus] -->|scrape annotated pod every 15s| Metrics
    KEDA[KEDA Prometheus scaler] -->|PromQL tool-call rate| Prometheus
    KEDA --> HPA[KEDA-generated HPA]
    HPA -->|Deployment /scale| Deployment[recsys-recommendation-mcp Deployment]
    Deployment --> Pods[1..3 Ready MCP pods]
```

#### Stage 1: completed MCP tool calls produce the signal

```python
TOOL_CALLS = Counter(
    "recsys_recommendation_mcp_tool_calls_total",
    "Recommendation MCP calls by terminal status.",
    ("status",),
)

@mcp.tool()
async def get_personalized_recommendations(...):
    response = await inference_client.recommend(...)
    TOOL_CALLS.labels(tool_result_status(response)).inc()
    return response.model_dump()
```

References:

- [Recommendation MCP metrics (line 5)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/observability.py#L5)
- [Recommendation MCP tool (line 40)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/server.py#L40)

#### Stage 2: Prometheus scrapes and KEDA queries tool-call rate

```yaml
# recsys-recommendation-mcp Deployment pod template
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/path: /metrics
  prometheus.io/port: "8080"

# The shared recsys-kubernetes-pods job includes namespace kagent and rewrites
# each scrape target from pod IP plus prometheus.io/port.
```

```yaml
query: 'sum(rate(recsys_recommendation_mcp_tool_calls_total[1m]))'
metricName: recsys_recommendation_mcp_requests_per_second
threshold: "1"
```

References:

- [MCP pod scrape annotations](../../../infra/helm/recsys-recommendation-mcp/templates/deployment.yaml#L27)
- [standalone Prometheus pod-discovery job](../../../infra/helm/recsys-observability/templates/prometheus.yaml#L192)
- [MCP Prometheus trigger (line 14)](../../../infra/helm/recsys-recommendation-mcp/templates/scaledobject.yaml#L14)

#### Stage 3: KEDA/HPA scales the stateless MCP Deployment

```yaml
scaleTargetRef:
  apiVersion: apps/v1
  kind: Deployment
  name: recsys-recommendation-mcp
minReplicaCount: 1
maxReplicaCount: 3
fallback:
  failureThreshold: 3
  replicas: 1
triggers:
  - type: prometheus
    metadata:
      metricName: recsys_recommendation_mcp_requests_per_second
      threshold: "1"
      query: sum(rate(recsys_recommendation_mcp_tool_calls_total[1m]))
```

FastMCP is configured with `stateless_http=True` and `json_response=True`, so
any Ready replica can serve a request. When Prometheus fails three consecutive
times, KEDA reports `Fallback=True` and maintains one MCP replica.

References:

- [MCP ScaledObject (line 1)](../../../infra/helm/recsys-recommendation-mcp/templates/scaledobject.yaml#L1)
- [Production MCP `1/3/1` (line 9)](../../../infra/helm/recsys-recommendation-mcp/values-gcp.yaml#L9)
- [Stateless FastMCP server (line 24)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/server.py#L24)

#### Stage 4: Deployment, probes, and RollingUpdate make replicas usable

The MCP chart uses zero-unavailable rolling updates, a PDB with
`minAvailable: 1`, topology spread, soft anti-affinity, startup/readiness/
liveness probes, and an immutable non-root container.

References:

- [MCP Deployment strategy (line 12)](../../../infra/helm/recsys-recommendation-mcp/templates/deployment.yaml#L12)
- [MCP PDB (line 1)](../../../infra/helm/recsys-recommendation-mcp/templates/pdb.yaml#L1)

### 1.4 Recommendation SandboxAgent autoscaling stages

```mermaid
flowchart LR
    A2A[Incoming A2A recommendation] --> ATE[ATE API assigns a worker]
    ATE --> Worker[Recommendation gVisor worker: assigned]
    ATE -->|expose ate_workerpool_workers| Metrics[ate-api-server :9090/metrics]
    Prometheus[recsys-prometheus] -->|scrape ate-system pods every 15s| Metrics
    KEDA[KEDA assigned-worker scaler] -->|PromQL max assigned| Prometheus
    KEDA --> HPA[KEDA-generated HPA]
    HPA -->|WorkerPool /scale| Pool[recsys-recommendation-sandbox-pool]
    Pool --> Controller[Substrate WorkerPool controller]
    Controller --> Workers[1..3 ateom-gvisor workers]
```

#### Stage 1: A2A sessions assign gVisor workers

The declarative agent profile binds to the dedicated WorkerPool:

```yaml
spec:
  type: Declarative
  substrate:
    workerPoolRef:
      apiGroup: ate.dev
      kind: WorkerPool
      name: recsys-recommendation-sandbox-pool
```

Reference: [Recommendation SandboxAgent WorkerPool binding (line 17)](../../../infra/helm/recsys-recommendation-agent/templates/sandboxagent.yaml#L17).

#### Stage 2: Prometheus measures assigned workers

`ate-api-server` owns the centralized worker registry and exports its state on
port `9090`. The upstream Substrate chart adds
`prometheus.io/scrape: "true"` and `prometheus.io/port: "9090"`; because no
path override is present, Prometheus uses `/metrics`. The standalone
`recsys-kubernetes-pods` job includes `ate-system` and scrapes both API
replicas. The query uses `max` to avoid double-counting their identical gauges.

```yaml
# live ate-api-server pod template rendered by the Substrate chart
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "9090"
containers:
  - name: ate-api-server
    ports:
      - name: prometheus
        containerPort: 9090
```

```promql
max(ate_workerpool_workers{
  ate_workerpool_namespace="kagent",
  ate_workerpool_name="recsys-recommendation-sandbox-pool",
  ate_worker_state="assigned"
})
```

References:

- [Substrate chart installation](../../../infra/terraform/gcp/modules/kubernetes-platform/kagent.tf#L125)
- [Prometheus `ate-system` target namespace](../../../infra/helm/recsys-observability/values.yaml#L75)
- [standalone Prometheus pod-discovery job](../../../infra/helm/recsys-observability/templates/prometheus.yaml#L192)

KEDA compares the maximum assigned-worker gauge with `AverageValue=0.7`.

#### Stage 3: HPA targets the WorkerPool `/scale` subresource

```yaml
scaleTargetRef:
  apiVersion: ate.dev/v1alpha1
  kind: WorkerPool
  name: recsys-recommendation-sandbox-pool
minReplicaCount: 1
maxReplicaCount: 3
fallback:
  failureThreshold: 3
  replicas: 1
```

References:

- [WorkerPool ScaledObject (line 5)](../../../infra/helm/recsys-recommendation-agent/templates/scaledobject.yaml#L5)
- [WorkerPool production values (line 1)](../../../infra/helm/recsys-recommendation-agent/values-gcp.yaml#L1)
- Substrate `0.0.11` supplies the native WorkerPool `/scale` selector through
  `.status.selector`; the `0.0.6` compatibility post-renderer was removed.

#### Stage 4: Substrate reconciles the generated gVisor Deployment

```text
KEDA-generated HPA
  -> WorkerPool/recsys-recommendation-sandbox-pool /scale
  -> Substrate WorkerPool controller
  -> recsys-recommendation-sandbox-pool
  -> 1..3 ateom-gvisor worker pods
```

Terraform owns the WorkerPool runtime and deliberately treats live replicas as
computed. The application Helm chart owns its ScaledObject and PDB. This avoids
Terraform overwriting KEDA's replica decisions.

References:

- [Terraform recommendation WorkerPool (line 335)](../../../infra/terraform/gcp/modules/kubernetes-platform/kagent.tf#L335)
- [WorkerPool PDB (line 1)](../../../infra/helm/recsys-recommendation-agent/templates/pdb.yaml#L1)
- [Runtime autoscale proof (line 151)](../../../ops/validation/recommendation_agentic_autoscale.sh#L151)

## 2. Recommendation Service

### 2.1 Recommendation input contract

```python
class RecommendationRequest(BaseModel):
    user_id: int = Field(ge=1)
    candidate_item_ids: list[int] | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )
    top_k: int = Field(default=10, ge=1, le=100)
```

Example request:

```json
{
  "user_id": 1001,
  "candidate_item_ids": null,
  "top_k": 3
}
```

When candidates are omitted, the inference API asks the online-feature service
for the candidate set. The MCP does not reproduce that logic.

References:

- [Inference request schema (line 6)](../../../apps/api-serving/inference-api/src/recsys_inference_api/schemas.py#L6)
- [MCP request policy (line 12)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/policy.py#L12)

### 2.2 Online features and Triton ownership

```python
online_features = await request.app.state.feature_service.fetch(
    OnlineFeaturesRequest(
        user_id=payload.user_id,
        candidate_item_ids=payload.candidate_item_ids,
        top_k=payload.top_k,
    )
)
response = await recommend_from_online_features(
    online_features=online_features,
    top_k=payload.top_k,
    route=route,
)
```

The inference service selects the A/B route, pulls online features, constructs
the Triton tensor payload, calls the BST ranker, and formats the response.

References:

- [Inference orchestration (line 96)](../../../apps/api-serving/inference-api/src/recsys_inference_api/app.py#L96)
- [Online-feature client (line 15)](../../../apps/api-serving/inference-api/src/recsys_inference_api/feature_client.py#L15)
- [Triton ranking path (line 147)](../../../apps/api-serving/inference-api/src/recsys_inference_api/ranking.py#L147)
- [A/B route selection (line 204)](../../../apps/api-serving/inference-api/src/recsys_inference_api/ab_testing.py#L204)

### 2.3 Model-ranked Top-K integrity

```python
pairs = sorted(
    zip(candidate_item_ids, [float(score) for score in scores]),
    key=lambda item: item[1],
    reverse=True,
)[:top_k]
```

The response preserves `user_id`, `model_version`, `ab_variant`,
`ab_experiment_id`, `item_id`, score, and order. The MCP validates and returns
that response without reranking. The agent only presents it.

References:

- [Top-K formatter (line 121)](../../../apps/api-serving/inference-api/src/recsys_inference_api/ranking.py#L121)
- [MCP pass-through response model (line 29)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/contracts.py#L29)
- [Ranking-integrity test (line 9)](../../../tests/integration/recommendation_agentic/test_inference_http_contract.py#L9)

### 2.4 MCP downstream boundary

```text
Recommendation SandboxAgent
  -> one recommendation MCP tool
  -> pooled HTTP POST /recommendations
  -> recsys-inference-api owns all serving logic
```

The MCP package contains no Feast, Redis, Triton, RAG, or context-agent runtime
dependency. It calls only the public inference API ClusterIP.

References:

- [Async inference client (line 22)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/client.py#L22)
- [MCP downstream ConfigMap (line 5)](../../../infra/helm/recsys-recommendation-mcp/templates/configmap.yaml#L5)
- [No-context-agent contract test (line 10)](../../../tests/e2e/recommendation_agentic/test_no_context_agent_dependency.py#L10)

## 3. FastAPI Proof

### 3.1 FastAPI application composition

```python
app = FastAPI(
    title="RecSys Recommendation MCP",
    version=__version__,
    lifespan=lifespan,
)
FastAPIInstrumentor.instrument_app(
    app,
    excluded_urls="healthz,ready,metrics",
)
app.mount("/", mcp.streamable_http_app())
```

FastAPI owns operational HTTP endpoints and middleware. FastMCP is mounted into
the same ASGI application and serves Streamable HTTP at `/mcp`.

References:

- [FastAPI composition root (line 20)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/app.py#L20)
- [Package definition (line 5)](../../../apps/agentic/recsys-recommendation-mcp/pyproject.toml#L5)

### 3.2 Data validation with Pydantic

```python
UserId = Annotated[int, Field(ge=1)]
CandidateList = Annotated[list[int], Field(min_length=1, max_length=500)]
CandidateItemIds = CandidateList | None
TopK = Annotated[int, Field(ge=1, le=100)]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
```

The same constraints appear in the checked-in tool contract. Schema drift is a
CI failure because the contract test compares Pydantic/FastMCP `tools/list` and
the SandboxAgent Helm render.

References:

- [MCP Pydantic contracts (line 9)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/contracts.py#L9)
- [Tool contract (line 7)](../../../configs/agentic/recsys-recommendation-agent/tools-contract.json#L7)
- [Cross-chart contract test (line 62)](../../../tests/contract/test_recommendation_agentic_contracts.py#L62)

### 3.3 Kubernetes health checks

```python
@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/ready")
async def ready() -> JSONResponse:
    if not settings.auth_token:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return JSONResponse({"status": "ready"})
```

```yaml
startupProbe: {httpGet: {path: /healthz, port: http}}
readinessProbe: {httpGet: {path: /ready, port: http}}
livenessProbe: {httpGet: {path: /healthz, port: http}}
```

References:

- [Health/readiness/version handlers (line 71)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/app.py#L71)
- [Kubernetes probes (line 59)](../../../infra/helm/recsys-recommendation-mcp/templates/deployment.yaml#L59)

### Image proof

![Recommendation MCP FastAPI and Kubernetes healthcheck proof](../../pngs/agent-recommendation-service-fastapi-kubernetes-healthchecks.png)

**Figure 1 — Recommendation MCP FastAPI and Kubernetes healthcheck proof.**
The live capture shows `RollingUpdate` with zero unavailable and one surge pod,
the startup/readiness/liveness probes, and successful `/healthz`, `/ready`, and
`/version` responses. The version response identifies the stateless
Streamable HTTP service and immutable Artifact Registry image digest; no bearer
token or Secret value is exposed.

### 3.4 Async proof

```python
@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        await inference_client.aclose()

async def get_personalized_recommendations(...):
    response = await inference_client.recommend(...)
```

```python
self.client = httpx.AsyncClient(
    base_url=base_url,
    timeout=httpx.Timeout(request_timeout_seconds),
    limits=httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
    ),
)
```

The client has one shared connection pool, a 15-second total deadline, and at
most one jittered retry for network errors or HTTP `502/503`.

References:

- [Async application lifecycle (line 36)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/app.py#L36)
- [Pooled async client and retry (line 36)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/client.py#L36)
- [Async integration test (line 9)](../../../tests/integration/recommendation_agentic/test_inference_http_contract.py#L9)

### 3.5 Get recommendations through the inference API

```python
response = await self.client.post(
    "/recommendations",
    json=payload,
    headers=headers,
)
return RecommendationResponse.model_validate(response.json())
```

Unlike the context-data MCP, this service intentionally does **not** use Feast
SDK directly. `recsys-inference-api` owns the online-feature call and Triton
ranking. This boundary removes an extra agent/LLM turn and keeps ranking logic
in the serving service.

Reference: [Inference API client request path (line 46)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/client.py#L46).

### Runtime image proof

![Recommendation API OpenAPI surface](../../pngs/agent-recommendation-service-api-openapi-surface.png)

**Figure 2 — Recommendation API OpenAPI surface.** The live Swagger UI exposes
`POST /recommendations` together with `/healthz`, `/ready`, `/version`, and
`/metrics`, plus the typed recommendation request/response schemas.

![Direct recommendation request and model-ranked response](../../pngs/agent-recommendation-service-direct-request-response.png)

**Figure 3 — Direct serving request.** Swagger records a successful HTTP `200`
request for `user_id=1` constrained to candidate item `0`. The response returns
the same item with its model score and the deployed model version. The null
A/B fields accurately show that this particular request was not assigned to an
experiment.

![Inference API baseline at one replica](../../pngs/agent-recommendation-service-inference-baseline-1-replica.png)

**Figure 6 — Inference API baseline.** K9s shows one Ready and Running
`recsys-inference-api` pod in namespace `api-serving` before load, with the
namespace, resource usage, pod IP, and node columns visible.

![Inference API scaled to three replicas](../../pngs/agent-recommendation-service-inference-scaleout-3-replicas.png)

**Figure 7 — Inference API scale-out.** Under recommendation load, K9s shows
three separate `2/2 Ready` and Running inference API pods with distinct pod IPs.
Paired with Figure 6, this proves scale-out from the configured minimum of one
to the maximum of three replicas.

## 4. MCP Server

### 4.1 MCP tool and transport

```python
mcp = FastMCP(
    "RecSys Recommendation MCP",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_hosts),
    ),
)
```

Exactly one tool is exposed:

```text
get_personalized_recommendations(
  user_id: int,
  candidate_item_ids: list[int] | null = null,
  top_k: int = 10
)
```

The `/mcp` route requires a bearer token and restricts browser origins. Invalid
Pydantic input fails before the downstream API call. Downstream failures are
serialized as typed, non-fabricated service errors.

References:

- [FastMCP server (line 24)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/server.py#L24)
- [MCP authentication middleware (line 53)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/app.py#L53)
- [Typed downstream errors (line 9)](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/errors.py#L9)

### 4.2 Kubernetes Deployment and zero-unavailable RollingUpdate

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 0
    maxSurge: 1
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
containers:
  - name: mcp
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: {drop: ["ALL"]}
```

The release also creates a ClusterIP Service, ServiceAccount, PDB,
NetworkPolicy, topology spreading, and soft pod anti-affinity. Metrics are
collected through the Deployment's `prometheus.io/*` pod annotations and the
standalone Prometheus pod-discovery job.

References:

- [MCP Deployment (line 12)](../../../infra/helm/recsys-recommendation-mcp/templates/deployment.yaml#L12)
- [MCP Service (line 1)](../../../infra/helm/recsys-recommendation-mcp/templates/service.yaml#L1)
- [MCP ServiceAccount (line 1)](../../../infra/helm/recsys-recommendation-mcp/templates/serviceaccount.yaml#L1)
- [MCP PDB (line 1)](../../../infra/helm/recsys-recommendation-mcp/templates/pdb.yaml#L1)
- [MCP NetworkPolicy (line 1)](../../../infra/helm/recsys-recommendation-mcp/templates/networkpolicy.yaml#L1)
- [MCP pod scrape annotations](../../../infra/helm/recsys-recommendation-mcp/templates/deployment.yaml#L27)
- [standalone Prometheus pod-discovery job](../../../infra/helm/recsys-observability/templates/prometheus.yaml#L192)
- [Immutable image (line 1)](../../../images/agentic/recsys-recommendation-mcp/Dockerfile#L1)

### 4.3 KEDA autoscale and scaler fallback

```yaml
autoscaling:
  minReplicas: 1
  maxReplicas: 3
  pollingInterval: 15
  cooldownPeriod: 120
  requestRateTarget: "1"
  fallback:
    failureThreshold: 3
    replicas: 1
```

References:

- [MCP ScaledObject template (line 1)](../../../infra/helm/recsys-recommendation-mcp/templates/scaledobject.yaml#L1)
- [MCP defaults (line 23)](../../../infra/helm/recsys-recommendation-mcp/values.yaml#L23)
- [MCP production placement and `1/3/1` (line 9)](../../../infra/helm/recsys-recommendation-mcp/values-gcp.yaml#L9)
- [MCP autoscale/fallback proof (line 89)](../../../ops/validation/recommendation_agentic_autoscale.sh#L89)

### Image proof

![Recommendation MCP baseline at one replica](../../pngs/agent-recommendation-service-mcp-baseline-1-replica.png)

**Figure 9 — MCP baseline.** K9s shows one Running
`recsys-recommendation-mcp` pod before load. Its `2/2 Ready` value represents
the MCP container plus Istio sidecar in one pod, not two replicas.

![Recommendation MCP scaled to three replicas](../../pngs/agent-recommendation-service-mcp-scaleout-3-replicas.png)

**Figure 10 — MCP scale-out.** During MCP request-rate load, the same K9s Pods
view shows three separate `2/2 Ready` and Running MCP pods. Paired with Figure
9, this proves KEDA-driven scale-out from one to three replicas.

## 5. SandboxAgent Uses MCP With Multi-Replica Autoscaling

### 5.1 RemoteMCPServer

```yaml
apiVersion: kagent.dev/v1alpha2
kind: RemoteMCPServer
metadata:
  name: recsys-recommendation-mcp
spec:
  protocol: STREAMABLE_HTTP
  url: http://recsys-recommendation-mcp.kagent.svc.cluster.local:8080/mcp
  timeout: 15s
  headersFrom:
    - name: Authorization
      valueFrom:
        type: Secret
        name: recsys-recommendation-mcp-auth
        key: Authorization
```

References:

- [RemoteMCPServer template (line 1)](../../../infra/helm/recsys-recommendation-agent/templates/remotemcpserver.yaml#L1)
- [MCP URL, timeout, Secret reference, and tool values (line 3)](../../../infra/helm/recsys-recommendation-agent/values.yaml#L3)

### 5.2 SandboxAgent profile

```yaml
apiVersion: kagent.dev/v1alpha2
kind: SandboxAgent
metadata:
  name: recsys-recommendation-agent-sandbox
spec:
  type: Declarative
  platform: substrate
  declarative:
    runtime: go
    modelConfig: default-model-config
    tools:
      - type: McpServer
        mcpServer:
          name: recsys-recommendation-mcp
          toolNames:
            - get_personalized_recommendations
```

The prompt requires one tool call, preserves service order and scores, forbids
LLM reranking, and forbids context-agent/RAG/feature tool calls.

Reference: [SandboxAgent, prompt, A2A skill, and one-tool binding (line 10)](../../../infra/helm/recsys-recommendation-agent/templates/sandboxagent.yaml#L10).

#### System prompt and MCP tool context

The Recommendation Agent receives both orchestration policy and the MCP
function contract. kagent assembles that model-visible context as follows:

```text
systemMessage
  + allowed toolNames
  + MCP tools/list description
  + generated inputSchema
  -> model tool context
```

These two core excerpts are copied verbatim from the current `systemMessage`.
Together they enforce exactly one recommendation call, preserve backend ranking,
and make the tool response terminal. The complete prompt remains in the linked
Helm values source.

```text
You are the RecSys recommendation presentation agent. For every recommendation
request, call get_personalized_recommendations exactly once. Do not call any
agent, context service, RAG tool, or feature tool. Preserve the returned item
order, item_id, score, model_version, A/B variant, and experiment metadata.
Never rerank with the language model. Empty items is a valid empty result.
If the service is unavailable, say so and do not invent recommendations.
```

```text
The recommendation tool is a terminal step. Once its function response is
present, your next response MUST be plain answer text with zero function
calls. The backend ranking is already the user's personalized result; never
ask what recommendation preference or explanation reason the user wants.
```

FastMCP exposes one tool. Its docstring becomes the `tools/list` description,
while the annotated argument types generate the input schema. The executable
body is omitted from this documentation excerpt.

```python
@mcp.tool()
async def get_personalized_recommendations(
    user_id: UserId,
    candidate_item_ids: CandidateItemIds = None,
    top_k: TopK = 10,
) -> dict[str, object]:
    """Get model-ranked Top-K items without changing order or scores.

    Args:
        user_id: Required positive integer copied from the user's request.
        candidate_item_ids: Optional list of 1-500 candidate item IDs, or null.
        top_k: Requested result count from 1-100; defaults to 10.

    Always provide ``user_id`` in the tool arguments. For example, a request
    for three items for user 1001 uses ``{"user_id": 1001, "top_k": 3}``.
    """
    ...
```

Prompt and tool-context references:

- [Complete Recommendation Agent system prompt](../../../infra/helm/recsys-recommendation-agent/values.yaml#L14)
- [Recommendation Agent one-tool allow-list](../../../infra/helm/recsys-recommendation-agent/values.yaml#L8)
- [FastMCP recommendation tool implementation and description](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/server.py#L40)
- [Annotated types used to generate the recommendation input schema](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/contracts.py#L9)
- [Canonical recommendation-tool JSON Schema contract](../../../configs/agentic/recsys-recommendation-agent/tools-contract.json#L1)

### 5.3 Terraform-owned dedicated gVisor WorkerPool

```hcl
resource "kubernetes_manifest" "recsys_recommendation_sandbox_pool" {
  manifest = {
    apiVersion = "ate.dev/v1alpha1"
    kind       = "WorkerPool"
    metadata = {
      name      = "recsys-recommendation-sandbox-pool"
      namespace = "kagent"
    }
    spec = {
      replicas      = 1
      ateomImage = "ghcr.io/kagent-dev/substrate/ateom-gvisor:v0.0.11"
    }
  }
  computed_fields = ["spec.replicas"]
}
```

References:

- [Dedicated recommendation WorkerPool (line 335)](../../../infra/terraform/gcp/modules/kubernetes-platform/kagent.tf#L335)
- [Pinned kagent/Substrate versions (line 1)](../../../infra/terraform/gcp/modules/kubernetes-platform/kagent.tf#L1)
- [KEDA WorkerPool `/scale` RBAC (line 431)](../../../infra/terraform/gcp/modules/kubernetes-platform/kagent.tf#L431)

### 5.4 KEDA scales WorkerPool from one to three workers

```yaml
autoscaling:
  metricMode: assignedWorkers
  minReplicas: 1
  maxReplicas: 3
  assignedWorkersPerReplica: "0.7"
  fallback:
    failureThreshold: 3
    replicas: 1
```

The scalable runtime is the WorkerPool and its generated Deployment—not the
declarative SandboxAgent CR.

References:

- [WorkerPool ScaledObject (line 5)](../../../infra/helm/recsys-recommendation-agent/templates/scaledobject.yaml#L5)
- [WorkerPool values `1/3/1` (line 41)](../../../infra/helm/recsys-recommendation-agent/values.yaml#L41)
- [Production WorkerPool override (line 1)](../../../infra/helm/recsys-recommendation-agent/values-gcp.yaml#L1)
- [WorkerPool PDB (line 1)](../../../infra/helm/recsys-recommendation-agent/templates/pdb.yaml#L1)
- [Autoscale validation (line 151)](../../../ops/validation/recommendation_agentic_autoscale.sh#L151)

### Autoscale image proof

![Recommendation Sandbox WorkerPool baseline at one replica](../../pngs/agent-recommendation-service-workerpool-baseline-1-replica.png)

**Figure 11 — Sandbox WorkerPool baseline.** K9s shows the single `1/1 Ready`
and Running `recsys-recommendation-sandbox-pool-deployment` pod after the
scale-down window. The `SandboxAgent` remains one declarative profile.

![Recommendation Sandbox WorkerPool scaled to three replicas](../../pngs/agent-recommendation-service-workerpool-scaleout-3-replicas.png)

**Figure 12 — Sandbox WorkerPool scale-out.** During concurrent A2A work, K9s
shows three separate `1/1 Ready` and Running gVisor worker pods. Paired with
Figure 11, this proves the WorkerPool—not the declarative SandboxAgent CR—scales
from one to three workers.

## 6. Sandbox Security

### 6.1 Domain-restricted sandbox network

```yaml
sandbox:
  network:
    allowedDomains:
      - recsys-recommendation-mcp.kagent.svc.cluster.local
```

The allow-list excludes the context agent, Feature/RAG MCP, inference API,
online-feature API, Triton, and public domains. The agent can reach only its
dedicated recommendation MCP.

References:

- [Single allowed domain (line 37)](../../../infra/helm/recsys-recommendation-agent/values.yaml#L37)
- [Sandbox network render (line 13)](../../../infra/helm/recsys-recommendation-agent/templates/sandboxagent.yaml#L13)

### 6.2 gVisor isolation and least privilege

```text
SandboxAgent platform=substrate
  -> WorkerPool ateomImage=ateom-gvisor:v0.0.11
  -> MCP container UID/GID 10001
  -> read-only root filesystem
  -> all Linux capabilities dropped
  -> NetworkPolicy permits only DNS, inference API, and observability egress
```

References:

- [gVisor WorkerPool (line 335)](../../../infra/terraform/gcp/modules/kubernetes-platform/kagent.tf#L335)
- [MCP container security (line 36)](../../../infra/helm/recsys-recommendation-mcp/templates/deployment.yaml#L36)
- [MCP NetworkPolicy (line 1)](../../../infra/helm/recsys-recommendation-mcp/templates/networkpolicy.yaml#L1)
- [RemoteMCPServer Secret reference (line 13)](../../../infra/helm/recsys-recommendation-agent/templates/remotemcpserver.yaml#L13)

### Runtime image-proof status

The final production autoscale run completed `2187/2187` concurrent A2A load
requests without error and proved `1 -> 2 -> 3 -> 2 -> 1`. Breaking the
Prometheus endpoint made KEDA report `Fallback=True` with desired replicas at
one; the validation trap restored the original endpoint and the scaler returned
to `Ready=True`, `Active=False`, `Fallback=False`.

## 7. Agent Registry and Governance

Jenkins publishes only after live MCP protocol smoke and SandboxAgent A2A smoke
succeed. Registry versions are derived from the same Git commit:

```text
recsys/recsys-recommendation-mcp
recsys/recsys-recommendation-agent-sandbox
version: 0.1.0+<12-character-git-sha>
tag:     0.1.0-<12-character-git-sha>
```

The SandboxAgent manifest declares the MCP artifact dependency. Publishing is
idempotent: a matching version/commit is a no-op, while mismatched metadata at
the same version fails the pipeline.

References:

- [Registry manifest generation (line 1026)](../../../jenkins/scripts/deploy/agentic.sh#L1026) and [recommendation publish actions (line 1331)](../../../jenkins/scripts/deploy/agentic.sh#L1331)
- [Registry runtime smoke (line 1)](../../../ops/validation/recommendation_agentic_registry_smoke.sh#L1)
- [Recommendation deploy units (line 260)](../../../jenkins/config/deploy-units.json#L260)

### Image proof

![Published recommendation MCP and version history](../../pngs/agent-recommendation-service-registry-mcp-version-history.png)

**Figure 13 — Governed MCP artifact.** Agent Registry Catalog shows the RecSys
Recommendation MCP artifact, its recommendation-only service description, and
two version tags with the 12-character Git-derived suffix. The immutable image
reference remains a Raw-metadata/terminal verification item because it is not
visible in this Overview capture.

![Published recommendation SandboxAgent and version history](../../pngs/agent-recommendation-service-registry-sandbox-agent-version-history.png)

**Figure 14 — Governed SandboxAgent artifact.** Agent Registry Catalog shows
`recsys-recommendation-agent-sandbox`, its gVisor/no-reranking description, and
the same two version tags as the MCP publication. The MCP dependency should be
verified in Technical or Raw metadata; it is not claimed as visible here.

## 8. Agent Chat UI

The Terraform-owned kagent installation supplies the UI and global
`default-model-config` backed by `qwen3.5-0.8b`. The application chart supplies
the recommendation SandboxAgent profile and its one-tool MCP binding. The
current shared model settings are deterministic (`temperature=0`, `seed=42`,
`maxTokens=384`); older screenshots that show a 256-token completion cap are
retained as historical evidence rather than the live configuration.

The acceptance conversation is:

```text
Recommend exactly 3 items for user_id=1001. Call
get_personalized_recommendations exactly once with candidate_item_ids=null and
top_k=3. Preserve service order and show item_id, score, model_version and A/B
metadata.
```

The tool history must contain exactly one
`get_personalized_recommendations` function call and one function response. The
final message must not invent product names, attributes, user preferences, or
RAG evidence.

References:

- [Recommendation SandboxAgent prompt (line 14)](../../../infra/helm/recsys-recommendation-agent/values.yaml#L14)
- [Single-tool contract (line 7)](../../../configs/agentic/recsys-recommendation-agent/tools-contract.json#L7)
- [A2A production smoke (line 337)](../../../jenkins/scripts/deploy/agentic.sh#L337)
- [No-context dependency test (line 10)](../../../tests/e2e/recommendation_agentic/test_no_context_agent_dependency.py#L10)

### Image proof

![Recommendation SandboxAgent ranked recommendations in kagent UI](../../pngs/agent-recommendation-service-kagent-ui-ranked-recommendations.png)

**Figure 15 — kagent Chat UI.** The selected
`recsys-recommendation-agent-sandbox` exposes one recommendation tool and
presents three model-ranked item IDs and scores for `user_id=900000` in the UI.
The collapsed capture does not expose the function-call arguments or
model/A-B metadata, so those remain terminal/tool-trace verification items.

## 9. CI/CD and Contract Evidence

| Component | Image | Deploy and governance units |
|---|---|---|
| `inference_api` | existing immutable inference image | inference API Helm release |
| `recommendation_mcp` | immutable `recsys-recommendation-mcp` image | MCP Helm release and MCP registry metadata |
| `recommendation_agent` | no custom image | SandboxAgent, RemoteMCPServer, WorkerPool KEDA/PDB, and agent registry metadata |

The recommendation components do not depend on `context_agent` or
`feature_rag_mcp`. The deployment order is:

```text
inference-api
  -> recommendation-mcp
  -> recommendation-agent
  -> recommendation-mcp-registry
  -> recommendation-agent-registry
```

PR gates include Ruff, compile/type checks, unit/integration/contract/e2e
tests, coverage, mutation tests for the new package, Helm lint/render,
kubeconform, and image build proof. Main builds by full Git SHA, resolves the
immutable digest, deploys with atomic Helm semantics, runs smoke tests, and only
then publishes registry metadata.

References:

- [Component routing (line 543)](../../../jenkins/config/components.json#L543)
- [Deploy DAG (line 260)](../../../jenkins/config/deploy-units.json#L260)
- [Image catalog (line 94)](../../../images/catalog.json#L94)
- [Recommendation CI gates (line 58)](../../../jenkins/scripts/ci/agentic.sh#L58)
- [Recommendation runtime verification (line 61)](../../../jenkins/scripts/test/agentic.sh#L61)
- [Contract tests (line 62)](../../../tests/contract/test_recommendation_agentic_contracts.py#L62)

## 10. Runtime Verification Commands

Run all commands from the repository root:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops
```

### 10.1 Run repository gates

```bash
make test-recommendation-agentic helm-recommendation-agentic
```

### 10.2 Verify deployed resources

```bash
make recommendation-agentic-preflight recommendation-agentic-smoke

kubectl -n kagent get \
  remotemcpserver/recsys-recommendation-mcp \
  sandboxagent/recsys-recommendation-agent-sandbox \
  workerpool/recsys-recommendation-sandbox-pool

# The recommendation agent must not depend on either resource.
kubectl -n kagent get sandboxagent recsys-recommendation-agent-sandbox -o yaml \
  | grep -E 'recsys-context-agent|recsys-feature-rag-mcp' && exit 1 || true
```

### 10.3 Health and grounded end-to-end smoke

Terminal A:

```bash
kubectl -n kagent port-forward service/recsys-recommendation-mcp 18087:8080
```

Terminal B:

```bash
curl -sS http://127.0.0.1:18087/healthz | jq
curl -sS http://127.0.0.1:18087/ready | jq
curl -sS http://127.0.0.1:18087/version | jq
```

Direct inference API proof:

```bash
kubectl -n api-serving port-forward service/recsys-inference-api 18086:80
```

```bash
curl -sS -X POST http://127.0.0.1:18086/recommendations \
  -H 'Content-Type: application/json' \
  -d '{"user_id":1001,"candidate_item_ids":null,"top_k":3}' | jq

curl -sS -X POST http://127.0.0.1:18086/recommendations \
  -H 'Content-Type: application/json' \
  -d '{"user_id":0,"candidate_item_ids":null,"top_k":101}' | jq
```

### 10.4 Autoscale and fallback

Inference API `1 -> 3`:

```bash
SERVICE=recsys-inference-api \
RECSYS_LOAD_TARGET=api \
LOCUST_USERS=180 \
LOCUST_SPAWN_RATE=60 \
LOCUST_DURATION=4m \
  bash ops/validation/serving_autoscale_load_test.sh
```

Recommendation MCP `1 -> 3`:

```bash
RECOMMENDATION_MCP_LOAD_SECONDS=120 \
RECOMMENDATION_MCP_LOAD_CONCURRENCY=8 \
RECOMMENDATION_SCALE_TIMEOUT_SECONDS=300 \
  bash ops/validation/recommendation_agentic_autoscale.sh mcp \
  | tee /tmp/recommendation-mcp-autoscale-proof.log
```

Recommendation WorkerPool `1 -> 3`:

```bash
RECOMMENDATION_SCALE_TIMEOUT_SECONDS=300 \
  bash ops/validation/recommendation_agentic_autoscale.sh worker \
  | tee /tmp/recommendation-worker-autoscale-proof.log
```

Prometheus-scaler fallback to one replica, with automatic restore through the
script trap:

```bash
RECOMMENDATION_PROVE_FALLBACK=true \
RECOMMENDATION_SCALE_TIMEOUT_SECONDS=300 \
  bash ops/validation/recommendation_agentic_autoscale.sh fallback \
  | tee /tmp/recommendation-fallback-proof.log
```

Expected fallback lines:

```text
recsys-recommendation-mcp fallback=True desired=1
recsys-recommendation-sandbox-pool fallback=True desired=1
```

Verify restoration:

```bash
kubectl -n kagent get scaledobject \
  recsys-recommendation-mcp \
  recsys-recommendation-sandbox-pool \
  -o custom-columns='NAME:.metadata.name,MIN:.spec.minReplicaCount,MAX:.spec.maxReplicaCount,READY:.status.conditions[?(@.type=="Ready")].status,FALLBACK:.status.conditions[?(@.type=="Fallback")].status'
```

### 10.5 Registry governance

```bash
make recommendation-agentic-registry
```

Expected terminal output confirms that both artifacts match the current full
Git commit.

### 10.6 Agent Chat UI and latency proof

Latency and no-context-agent proof:

```bash
RECOMMENDATION_LATENCY_REQUESTS=20 \
RECOMMENDATION_AGENT_LATENCY_REQUESTS=3 \
  make recommendation-agentic-latency

jq . reports/agentic/recommendation-latency.json
```

The JSON must report `no_context_agent_call: true`, direct API p95, MCP p95,
MCP overhead, and agent p95.

## 11. Submission Accuracy Notes

- Say **one recommendation SandboxAgent profile backed by 1–3 dedicated
  WorkerPool replicas**, not “the SandboxAgent CR has three replicas.”
- Say **KEDA targets the WorkerPool `/scale` subresource**, not the declarative
  SandboxAgent.
- Say **the recommendation MCP calls `recsys-inference-api`**, not Feast,
  Redis, Triton, Feature/RAG MCP, or context-agent directly.
- Say **the inference service owns online-feature retrieval, A/B routing,
  Triton scoring, and Top-K**.
- Say **the LLM presents the model-ranked response without reranking**.
- Say **Agent Registry supplies governance, version history, and discovery**;
  runtime chat traffic is not routed through Agent Registry.
- `2/2` in an Istio-injected MCP pod means two containers in one pod; it does
  not mean two replicas.
- Do not label the context/RAG agent as a recommendation dependency. The
  contract and latency proof explicitly require no context-agent call.
