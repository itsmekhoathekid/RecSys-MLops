# Approved contract migration: diagnostic result

The user authorized a shared contract/guardrail change and a new baseline on
2026-09-09. `workflow_contract.py` creates a separate, create-only migration
artifact. It preserves generation, LLM identity, tool bindings, and historical
releases. The added prompt rules require exact JSON, clarification when the
user ID is missing, valid empty results, and no repeated dependency calls.
These are **prompt constraints, not a hard execution guard**.

Prepared baseline `b8ad3ade3b619b1a140ee2efa5e6b5258decc4f75aa80fba35d7c8e1c3c7c1f2`
derives from snapshot `85d74972a5538e3754cb66b062f94fc732c50a6425fdf379d48a050eb6b17cdc`.
It has not been accepted, published, deployed or activated. Source checksum,
parent identity and contract checksum are recorded in
`.llm-agent-cd/workflow-contract-v1-20260909/migration.json`.
Live revisions must be read again before any eventual activation: this local
artifact is not permission to skip snapshot freshness or the Jenkins lock.

## Real compatibility checks

Exactly six new requests per backend, using the same fixture inputs and
assertions as before; only the explicitly versioned system contract changed.
The requests used fake tool responses and did not execute real tools.
The baseline migration's full three-role prompts have not yet been exercised
through A2A; these six tests are only a necessary isolated compatibility check.

| Backend | Result | Blocking behavior |
|---|---|---|
| Qwen2.5 0.5B, fixed template v2 | 3/6 | Fabricates user_id=1, returns prose, invents an error for empty items |
| Current Qwen3.5 0.8B | 5/6 | Empty items leads to another get_recommendations call |

Neither backend is accepted for this contract. No request was retried and no
assertion was weakened. In particular, rejecting a duplicate tool request is
not itself evidence that the underlying model passed the no-duplicate gate.

## Safe current state and remaining work

- Small-model diagnostic Deployments are scaled to zero; champion and Istio
  routing remain unchanged. Airflow/DataHub/MLflow remain stopped as instructed.
- Zero of the 20 A/B conversations were executed. Dispatch remains disabled.
- Hard before-tool enforcement is **not implemented**: the A2A router currently
  receives completed task evidence, too late to prevent a second tool call.
  It would be incorrect to claim that stronger prompts or post-execution
  assertions provide an execution guard.
- Next implementation needs a supported before-tool/runtime interception
  boundary with trusted turn identity and exactly-once invocation correlation.
  It must retain visible violation evidence, not erase a failed model action or
  mark a blocked action as model compliance. Terminal deterministic rendering,
  if introduced, must be a shared, versioned workflow policy and separately
  tested, not answer repair inside the evaluator.
- Do not start the 20-case suite or promote this baseline while the prerequisite
  compatibility gate fails. The existing end-to-end activation, collector and
  dispatch checks remain required after that gate is resolved.

Redacted outputs: `evidence/workflow-contract-v1-2026-09-09.json`.
