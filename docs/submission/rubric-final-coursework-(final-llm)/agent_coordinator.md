# Coordinator Regular Agent with A2A and MCP Routing

`recsys-coordinator-agent` is a declarative kagent `Agent` with exactly one
replica. It is intentionally not a `SandboxAgent`: orchestration concurrency is
handled by the regular Agent Deployment while the context and recommendation
specialists remain isolated in their own Substrate WorkerPools.

```text
kagent Chat UI / A2A client
              |
              v
Agent/recsys-coordinator-agent (Deployment replicas=1)
  |-- A2A --> recsys-context-agent-sandbox
  |-- A2A --> recsys-recommendation-agent-sandbox
  |-- MCP --> recsys-feature-rag-mcp
  `-- MCP --> recsys-recommendation-mcp
```

## Runtime contract

The public interfaces are:

- A2A: `/api/a2a/kagent/recsys-coordinator-agent/`
- Registry: `recsys/recsys-coordinator-agent`

The Helm chart renders only one regular `Agent`; it renders no coordinator
`SandboxAgent`, `WorkerPool`, `ScaledObject`, or PodDisruptionBudget.

```yaml
apiVersion: kagent.dev/v1alpha2
kind: Agent
metadata:
  name: recsys-coordinator-agent
  namespace: kagent
spec:
  type: Declarative
  declarative:
    modelConfig: default-model-config
    deployment:
      replicas: 1
    tools:
      - type: Agent
        agent:
          apiGroup: kagent.dev
          kind: SandboxAgent
          name: recsys-context-agent-sandbox
      - type: Agent
        agent:
          apiGroup: kagent.dev
          kind: SandboxAgent
          name: recsys-recommendation-agent-sandbox
      - type: McpServer
        mcpServer:
          name: recsys-feature-rag-mcp
      - type: McpServer
        mcpServer:
          name: recsys-recommendation-mcp
```

References:

- [regular Agent template](../../../infra/helm/recsys-coordinator-agent/templates/agent.yaml)
- [prompt and provider configuration](../../../infra/helm/recsys-coordinator-agent/values.yaml)
- [tool contract](../../../configs/agentic/recsys-coordinator-agent/tools-contract.json)

## Routing behavior

The prompt keeps the existing routing rules:

```text
Context, feature, exact-chunk, or RAG request
  -> context specialist over A2A

Ranked recommendation request
  -> recommendation specialist over A2A

Recommendation plus grounded explanation
  -> both specialists, then combine without reranking

Explicit raw-tool or independent-verification request
  -> invoke the requested MCP provider directly

Unavailable dependency
  -> retain valid partial results, identify the failed source, never guess
```

The coordinator preserves the recommendation service's ordering, scores,
model version, and experiment metadata. Grounded claims cite the specialist's
returned chunk identifiers.

## Fixed-replica concurrency proof

Coordinator autoscaling was removed. The replacement validation sends
concurrent A2A requests while sampling the Deployment and fails if desired or
ready replicas differ from one:

```bash
make coordinator-agentic-concurrency
```

The production run on 2026-08-26 completed eight requests at concurrency four
and kept the coordinator Deployment at `1/1`. The normal smoke also returned
`COORDINATOR-OK` through the regular A2A endpoint.

References:

- [concurrency validation](../../../ops/validation/coordinator_agentic_concurrency.sh)
- [A2A and MCP smoke](../../../ops/validation/coordinator_agentic_smoke.sh)
- [runtime contract tests](../../../tests/contract/test_coordinator_agentic_contracts.py)

## Registry publication

Jenkins publishes the regular identity only after it verifies both specialist
Agent artifacts and both MCP artifacts at the same immutable Git SHA. It then
verifies the new artifact before retiring
`recsys/recsys-coordinator-agent-sandbox`.

The 2026-08-26 live Registry inspection found the dependency set only at commit
`8e82cfdbb42d5c94fc6bd58ef19fd75af0d9478b`, while the regular coordinator
change was still uncommitted. Publishing was therefore deliberately withheld:
using the current `HEAD` would claim source provenance that does not contain the
regular Agent manifest. After these changes are committed and the four
dependencies are published at that SHA, run:

```bash
make coordinator-agentic-registry
```

The legacy sandbox artifact must remain until that command verifies the new
regular artifact. This is a release gate, not a runtime blocker; the live
regular Agent and its A2A route are already healthy.

Reference: [registry publish and dependency gates](../../../jenkins/scripts/deploy/agentic.sh).

## Substrate 0.0.11 rollout result

The coordinator migration succeeded independently of the Substrate upgrade.
The same maintenance window proved on a canary that GKE beta certificate APIs,
mTLS projected credentials, trust bundles, and
`ate_workerpool_workers` work with Substrate `0.0.11`. Production then failed
the kagent `0.9.9` compatibility gate with:

```text
grpc: error unmarshalling request: proto: cannot parse invalid wire-format data
```

Per the rollback plan, production specialists returned to Substrate `0.0.6`
and CPU-based WorkerPool autoscaling. The GKE `1.35.7` upgrade and one-way beta
API enablement remain. The coordinator stays a regular fixed-replica Agent and
does not depend on either Substrate version.

See [rollout validation and rollback evidence](validation_verification.md).
