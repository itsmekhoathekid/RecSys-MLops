# Sandbox-only Agent kéo Online Feature và RAG context

## Kiến trúc được triển khai

```text
kagent Chat UI
  -> SandboxAgent/recsys-context-agent-sandbox
  -> WorkerPool/recsys-context-sandbox-pool (KEDA 2..6 gVisor workers)
  -> RemoteMCPServer/recsys-feature-rag-mcp
  -> recsys-feature-rag-mcp
       -> recsys-online-feature-api
       -> recsys-rag-api
```

Không có regular `Agent` hoặc Agent Deployment trong application release. Agent
duy nhất chạy qua Agent Substrate/gVisor; KEDA target trực tiếp scale subresource
của `ate.dev/v1alpha1 WorkerPool`.

## Reference code và config

| Requirement | Reference |
|---|---|
| FastAPI, health/readiness, MCP ASGI | [`app.py`](../../../apps/agentic/recsys-feature-rag-mcp/src/recsys_feature_rag_mcp/app.py) |
| Pydantic MCP input/output | [`contracts.py`](../../../apps/agentic/recsys-feature-rag-mcp/src/recsys_feature_rag_mcp/contracts.py) |
| Async HTTP clients, retry và timeout | [`clients/`](../../../apps/agentic/recsys-feature-rag-mcp/src/recsys_feature_rag_mcp/clients) |
| Bốn MCP tools | [`server.py`](../../../apps/agentic/recsys-feature-rag-mcp/src/recsys_feature_rag_mcp/server.py) |
| Tool contract source of truth | [`tools-contract.json`](../../../configs/agentic/recsys-context-agent/tools-contract.json) |
| SandboxAgent, RemoteMCPServer, WorkerPool KEDA và PDB | [`recsys-kagent-agent`](../../../infra/helm/recsys-kagent-agent) |
| Terraform-owned Substrate WorkerPool baseline | [`configs/kagent/values.yaml`](../../../configs/kagent/values.yaml) |
| Jenkins preflight, registry publish và legacy cleanup | [`agentic.sh`](../../../jenkins/scripts/deploy/agentic.sh) |
| Runtime deployment checks | [`agentic.sh`](../../../jenkins/scripts/test/agentic.sh) |
| Autoscale và fallback proof | [`agentic_context_autoscale.sh`](../../../ops/validation/agentic_context_autoscale.sh) |
| Contract tests | [`test_agentic_context_contracts.py`](../../../tests/contract/test_agentic_context_contracts.py) |

## Kubernetes behavior

- `SandboxAgent/recsys-context-agent-sandbox` dùng Go ADK, model
  `qwen3.5-0.8b`, RemoteMCPServer và network allow-list chỉ tới MCP service.
- `WorkerPool/recsys-context-sandbox-pool` có Terraform-owned baseline hai gVisor
  workers. Application chart không adopt hoặc thay đổi Helm ownership của pool.
- KEDA `ScaledObject/recsys-context-sandbox-pool` target WorkerPool `/scale`, min
  2, max 6 và fallback 2 sau ba scaler failures.
- Prometheus scaler đo CPU của container `ateom` trong các pod
  `recsys-context-sandbox-pool-deployment-*`.
- CPU target là `500` microcores per worker, tương đương `0.0005` core.
  PromQL nhân số core với `1,000,000` trước khi trả external metric để tránh
  Kubernetes `resource.Quantity` làm tròn mất precision. Production load proof
  đo khoảng `1,000` microcores per worker dưới 20 concurrent A2A requests,
  trong khi idle tổng khoảng `52`, nên scaler không scale khi rảnh nhưng vượt
  target khi tải.
- PDB chọn label `ate.dev/worker-pool=recsys-context-sandbox-pool` và giữ tối
  thiểu một worker available khi có voluntary disruption.

## CI/CD và governance

Release và deploy DAG vẫn giữ tên hiện hữu để Helm upgrade tại chỗ:

```text
feature-rag-mcp
  -> context-agent
  -> feature-rag-mcp-registry
  -> context-agent-registry
```

Sau khi SandboxAgent readiness, grounded A2A smoke và registry publish thành
công, Jenkins backup mọi tag của `recsys/recsys-context-agent`, chạy
`arctl delete agent recsys/recsys-context-agent --all-tags`, rồi xác nhận catalog
chỉ còn sandbox identity. Backup và cleanup metadata được archive trong
`.ci-deploy/` để hỗ trợ audit hoặc operational rollback.

## Evidence commands

```bash
make test-agentic helm-agentic
AGENTIC_SMOKE_CHUNK_ID='800080:review:rev_800080_02:0' make agentic-smoke
make agentic-autoscale-test
make agentic-registry-smoke

kubectl -n kagent get sandboxagent,workerpool,scaledobject,hpa
kubectl -n kagent get deployment recsys-context-sandbox-pool-deployment
kubectl -n kagent get agent recsys-context-agent  # expected: NotFound
kubectl -n kagent get deployment recsys-context-agent  # expected: NotFound
```

Runtime scripts ghi grounded A2A, autoscale, fallback và registry evidence vào
`reports/agentic/`; Jenkins archive immutable release evidence từ `.ci-deploy/`.
