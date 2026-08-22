# Recommendation MCP Tool and SandboxAgent

## Architecture and latency boundary

The recommendation agent is deliberately independent from the context/RAG
agent. A UI request produces one recommendation MCP tool call and one existing
inference API request. The inference API remains the owner of online-feature
lookup, A/B routing, Triton/BST invocation, scoring and Top-K ordering.

```text
kagent Chat UI
  -> SandboxAgent/recsys-recommendation-agent-sandbox
  -> RemoteMCPServer/recsys-recommendation-mcp
  -> recsys-recommendation-mcp
  -> POST recsys-inference-api/recommendations
  -> online-feature-api + KServe/Triton BST
```

There is no `type: Agent` tool, context-agent A2A call, feature/RAG MCP call,
direct Feast/Redis/Triton access, or LLM reranking in this path.

## FastAPI, Pydantic and async MCP facade

The FastAPI composition exposes the Kubernetes probes, build identity,
Prometheus metrics and stateless Streamable HTTP MCP transport:

```python
app = FastAPI(title="RecSys Recommendation MCP", version=__version__, lifespan=lifespan)

@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/ready")
async def ready() -> JSONResponse:
    if not settings.auth_token:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return JSONResponse({"status": "ready"})

app.mount("/", mcp.streamable_http_app())
```

Reference:

- [`app.py`](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/app.py)
- [`settings.py`](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/settings.py)

The public tool input is constrained to `user_id >= 1`, 1–500 candidate IDs,
and `top_k` 1–100. The response intentionally contains only service-owned
fields and preserves item order and score.

```python
CandidateList = Annotated[list[ItemId], Field(min_length=1, max_length=500)]
TopK = Annotated[int, Field(ge=1, le=100)]

class RecommendationResponse(StrictModel):
    user_id: UserId
    model_version: str
    ab_variant: str | None = None
    ab_experiment_id: str | None = None
    items: list[RecommendationItem]
```

Reference:

- [`contracts.py`](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/contracts.py)
- [`tools-contract.json`](../../../configs/agentic/recsys-recommendation-agent/tools-contract.json)
- Existing inference contract: [`schemas.py`](../../../apps/api-serving/inference-api/src/recsys_inference_api/schemas.py)

The shared async HTTPX client pools keep-alive connections, propagates tracing,
uses a 15-second total deadline and retries at most once only for transport
errors or HTTP 502/503. It calls only `/recommendations`.

```python
async with asyncio.timeout(self.total_deadline_seconds):
    return await self._recommend_with_retry(payload)

response = await self.client.post("/recommendations", json=payload, headers=headers)
return RecommendationResponse.model_validate(response.json())
```

Reference: [`client.py`](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/client.py)

## One-tool MCP contract

```python
@mcp.tool()
async def get_personalized_recommendations(
    user_id: UserId,
    candidate_item_ids: CandidateItemIds = None,
    top_k: TopK = 10,
) -> dict[str, object]:
    response = await inference_client.recommend(...)
    return response.model_dump()
```

Reference:

- [`server.py`](../../../apps/agentic/recsys-recommendation-mcp/src/recsys_recommendation_mcp/server.py)
- Unit/protocol tests: [`test_server.py`](../../../tests/unit/agentic/recommendation_mcp/test_server.py), [`test_app.py`](../../../tests/unit/agentic/recommendation_mcp/test_app.py)
- HTTP integration proof: [`test_inference_http_contract.py`](../../../tests/integration/recommendation_agentic/test_inference_http_contract.py)

## Kubernetes deployment, RollingUpdate and autoscale fallback

The MCP release starts at one replica and scales to three on MCP tool request
rate. Rolling updates keep the only available replica during rollout and KEDA
falls back to one when Prometheus is unavailable.

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate: {maxUnavailable: 0, maxSurge: 1}
---
minReplicaCount: 1
maxReplicaCount: 3
fallback: {failureThreshold: 3, replicas: 1}
```

References include both default and production values:

- MCP [`values.yaml`](../../../infra/helm/recsys-recommendation-mcp/values.yaml), [`values-gcp.yaml`](../../../infra/helm/recsys-recommendation-mcp/values-gcp.yaml)
- [`deployment.yaml`](../../../infra/helm/recsys-recommendation-mcp/templates/deployment.yaml)
- [`scaledobject.yaml`](../../../infra/helm/recsys-recommendation-mcp/templates/scaledobject.yaml)
- [`pdb.yaml`](../../../infra/helm/recsys-recommendation-mcp/templates/pdb.yaml)
- [`networkpolicy.yaml`](../../../infra/helm/recsys-recommendation-mcp/templates/networkpolicy.yaml)
- Immutable image: [`Dockerfile`](../../../images/agentic/recsys-recommendation-mcp/Dockerfile)

## Recommendation-only SandboxAgent

The agent has exactly one MCP server/tool. Its prompt forbids agent calls,
context/RAG sources, invented attributes and reranking. gVisor network access is
allowlisted only for the recommendation MCP service.

```yaml
allowedDomains:
  - recsys-recommendation-mcp.kagent.svc.cluster.local
tools:
  - type: McpServer
    mcpServer:
      name: recsys-recommendation-mcp
      toolNames: [get_personalized_recommendations]
```

Reference:

- Agent [`values.yaml`](../../../infra/helm/recsys-recommendation-agent/values.yaml), [`values-gcp.yaml`](../../../infra/helm/recsys-recommendation-agent/values-gcp.yaml)
- [`sandboxagent.yaml`](../../../infra/helm/recsys-recommendation-agent/templates/sandboxagent.yaml)
- [`remotemcpserver.yaml`](../../../infra/helm/recsys-recommendation-agent/templates/remotemcpserver.yaml)

## Dedicated WorkerPool and KEDA

Terraform owns the dedicated gVisor WorkerPool and ignores live `spec.replicas`
drift. The application chart owns the KEDA ScaledObject and PDB. KEDA can update
the WorkerPool `/scale` subresource through dedicated RBAC.

```yaml
scaleTargetRef:
  apiVersion: ate.dev/v1alpha1
  kind: WorkerPool
  name: recsys-recommendation-sandbox-pool
minReplicaCount: 1
maxReplicaCount: 3
fallback: {failureThreshold: 3, replicas: 1}
```

Reference:

- Terraform WorkerPool/RBAC: [`kagent.tf`](../../../infra/terraform/gcp/kagent.tf)
- WorkerPool autoscale: [`scaledobject.yaml`](../../../infra/helm/recsys-recommendation-agent/templates/scaledobject.yaml)
- Worker PDB: [`pdb.yaml`](../../../infra/helm/recsys-recommendation-agent/templates/pdb.yaml)
- Cross-chart proof: [`test_recommendation_agentic_contracts.py`](../../../tests/contract/test_recommendation_agentic_contracts.py)

## Secrets and authorization

Vault bootstrap generates a distinct bearer token, External Secrets materializes
`recsys-recommendation-mcp-auth`, and Istio permits only the MCP service account
to reach the inference API.

Reference:

- [`bootstrap_vault.sh`](../../../ops/gcp/bootstrap_vault.sh)
- Security [`values.yaml`](../../../infra/helm/recsys-security/values.yaml)
- [`externalsecrets.yaml`](../../../infra/helm/recsys-security/templates/externalsecrets.yaml)
- [`istio-authorization.yaml`](../../../infra/helm/recsys-security/templates/istio-authorization.yaml)

## CI/CD and Agent Registry governance

The repository catalogs define independent `recommendation_mcp` and
`recommendation_agent` components. Neither depends on context/RAG. Main deploys
immutable image digests through this ordered graph:

```text
inference-api -> recommendation-mcp -> recommendation-agent
              -> recommendation-mcp-registry -> recommendation-agent-registry
```

The registry identities are `recsys/recsys-recommendation-mcp` and
`recsys/recsys-recommendation-agent-sandbox`, versioned from the Git SHA and
published only after MCP and A2A smoke tests.

Reference:

- [`components.json`](../../../jenkins/config/components.json)
- [`deploy-units.json`](../../../jenkins/config/deploy-units.json)
- [`catalog.json`](../../../images/catalog.json)
- CI helpers: [`agentic.sh`](../../../jenkins/scripts/ci/agentic.sh)
- Deploy/registry helpers: [`agentic.sh`](../../../jenkins/scripts/deploy/agentic.sh)
- Production verification: [`agentic.sh`](../../../jenkins/scripts/test/agentic.sh)

## Commands and evidence to capture

```bash
make test-recommendation-agentic helm-recommendation-agentic
make recommendation-agentic-preflight recommendation-agentic-smoke
bash ops/validation/recommendation_agentic_autoscale.sh all
RECOMMENDATION_PROVE_FALLBACK=true \
  bash ops/validation/recommendation_agentic_autoscale.sh all
make recommendation-agentic-registry
```

Capture the following evidence after the main deployment:

1. k9s `deploy` view showing `recsys-recommendation-mcp` changing `1/1` to
   `3/3` and `recsys-recommendation-sandbox-pool-deployment` changing `1/1`
   to `3/3` while the load script runs.
2. k9s `scaledobject` and `hpa` views showing min/max `1/3`, healthy Prometheus
   triggers, and fallback proof output after the scaler address is restored.
3. k9s `sandboxagent`, `remotemcpserver` and `workerpool` YAML showing Ready
   status, the one-tool list, gVisor image, `/scale` selector and allowlist.
4. Agent Registry Catalog pages for both identities with the same 12-character
   Git SHA; capture the details page rather than only the Deployed tab.
5. kagent UI conversation for `user_id=1001`, including returned item IDs,
   scores, model version and A/B metadata in unchanged service order.
6. Trace/latency dashboard showing one MCP call and one inference call, with no
   context-agent or RAG span.
