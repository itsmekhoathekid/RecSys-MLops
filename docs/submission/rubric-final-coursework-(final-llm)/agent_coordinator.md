# Coordinator SandboxAgent with assigned-worker autoscaling

The repository target is `SandboxAgent/recsys-coordinator-agent-sandbox`. It
has a dedicated Substrate `0.0.11` WorkerPool and the same assigned-worker KEDA
policy as the Context and Recommendation specialists.

```text
kagent Chat UI / sandbox A2A client
              |
              v
SandboxAgent/recsys-coordinator-agent-sandbox
  |-- WorkerPool --> recsys-coordinator-sandbox-pool (1..3)
  |-- A2A --------> recsys-context-agent-sandbox
  |-- A2A --------> recsys-recommendation-agent-sandbox
  |-- MCP --------> recsys-feature-rag-mcp
  `-- MCP --------> recsys-recommendation-mcp
```

## Runtime and public interfaces

- A2A: `/api/a2a-sandboxes/kagent/recsys-coordinator-agent-sandbox/`
- Registry: `recsys/recsys-coordinator-agent-sandbox`
- WorkerPool: `recsys-coordinator-sandbox-pool`
- Model configuration revision:
  `substrate-0.0.11-kagent-e6df917-assigned-workers-v22`
- kagent compatibility image: `0.10.0-e6df917-substrate0011-v7`

The Helm release renders a `SandboxAgent`, `ScaledObject`, and
`PodDisruptionBudget`. Terraform owns the WorkerPool's immutable runtime fields;
KEDA owns `.spec.replicas` through the native WorkerPool `/scale` subresource.
The previous regular `Agent/recsys-coordinator-agent` topology and its fixed
one-replica concurrency proof are superseded in source control.

References:

- [SandboxAgent template](../../../infra/helm/recsys-coordinator-agent/templates/sandboxagent.yaml)
- [assigned-worker scaler](../../../infra/helm/recsys-coordinator-agent/templates/scaledobject.yaml)
- [prompt and values](../../../infra/helm/recsys-coordinator-agent/values.yaml)
- [tool contract](../../../configs/agentic/recsys-coordinator-agent/tools-contract.json)

## Autoscaling contract

Production values select `metricMode: assignedWorkers`. The CPU mode remains
available only as a values-only rollback path.

```promql
max(
  ate_workerpool_workers{
    ate_workerpool_namespace="kagent",
    ate_workerpool_name="recsys-coordinator-sandbox-pool",
    ate_worker_state="assigned"
  }
)
```

The scaler uses `AverageValue=0.7`, replicas `1..3`, polling every 15 seconds,
immediate scale-up, a 300-second scale-down stabilization/cooldown, and fallback
to one replica after three Prometheus failures. `ignoreNullValues=false` and the
query intentionally has no `or vector(0)`, so a missing metric remains visible.

Run the production proof only after Substrate, Valkey, all three WorkerPools,
and Prometheus are healthy:

```bash
make coordinator-agentic-autoscale
```

The script captures idle `1`, assigned-work scale-out `1 -> 2 -> 3`, return to
`1` after the cooldown, and the one-replica KEDA fallback. It restores the
Prometheus endpoint even when the validation fails.

## Routing behavior and smoke test

The prompt retains these routing rules:

- context, feature, exact-chunk, or RAG requests use the Context specialist;
- ranked recommendations use the Recommendation specialist;
- grounded recommendations use both specialists without reranking;
- explicitly requested raw/verification operations may call MCP directly;
- partial failures preserve valid results and identify the unavailable source.

```bash
make coordinator-agentic-smoke
```

The smoke covers context-only, recommendation-only, composite, direct MCP, and
partial-specialist-failure cases through the sandbox A2A endpoint using
`SendMessage` and `A2A-Version: 1.0`.

Both specialist Agent tools set `isolateSessions: true`. Every delegation gets
a fresh child A2A context, so a previous `INPUT_REQUIRED` or failed child task
cannot contaminate a later coordinator request. The production smoke uses one
bounded attempt; its 1,800-second timeout accommodates measured local-model
latency without starting duplicate server-side work.
For delegated cases, the gate also inspects the child Agent response and rejects
typed downstream failures such as `http_422`, tool-execution errors, or a
silently degraded "source unavailable" answer. A completed parent task alone
is therefore not sufficient evidence of successful specialist routing.

The v7 runtime adds a per-invocation exact-call guard for this Coordinator. An
explicitly requested specialist or direct MCP tool executes once, tool results
survive model retries, and a composite request advances from Recommendation to
Context instead of repeating the first specialist. It accumulates sequential
direct-tool responses and synthesizes the required partial result if the small
model emits an empty or duplicate terminal turn. Generic prompts remain
model-routed and are not forced into a tool call.

Coordinator v22 makes the partial-result response deterministic. After a
missing Context chunk and a successful Recommendation call, the final answer
must retain the first returned `item_id` and explicitly label Context
unavailable. Jenkins rejects a completed task that drops either part.

## Registry and Jenkins release gate

Jenkins publishes the sandbox coordinator only after both specialist artifacts
and both MCP artifacts exist at the same immutable Git SHA and routing smoke is
green. Only then may it retire `recsys/recsys-coordinator-agent`.

```bash
make coordinator-agentic-registry
```

The component contract now waits for the Coordinator SandboxAgent, WorkerPool,
ScaledObject, HPA, and native WorkerPool Deployment. The autoscale proof is a
production verification step, not a Helm render substitute.

## Current production evidence status

The 27 August 2026 production run is green on Substrate `0.0.11` mTLS and the
v7 kagent compatibility image. The Coordinator WorkerPool proved
`1 -> 2 -> 3 -> 2 -> 1`; an intentionally invalid Prometheus endpoint made
KEDA report fallback while desired/available replicas remained `1/1`, and the
endpoint was restored automatically. The full v19 routing suite passed all
six cases and remains the complete-suite baseline, with composite evidence showing exactly
`Recommendation Agent -> Context Agent`, once each. Substrate control-plane,
Valkey, RustFS, the three SandboxAgents, WorkerPools, ScaledObjects, and HPAs
were healthy after restoration.

Coordinator v21 targeted production evidence additionally proves a fresh child
session, exact parent JSON `top_k=1`, exact child MCP `top_k=1`, no `ask_user`,
and `TASK_STATE_COMPLETED` in
`reports/agentic/recsys-coordinator-agent-sandbox-recommendation-v24.json`.
The v19 full-suite evidence remains
`reports/agentic/recsys-coordinator-agent-sandbox-a2a-v19.json`; autoscale load
evidence is `reports/agentic/coordinator-autoscale-load.json`.

Historical `0.0.6`, regular-Coordinator, and failed `0.0.11` evidence remains in
[Validation and Verification](validation_verification.md) as superseded history.
