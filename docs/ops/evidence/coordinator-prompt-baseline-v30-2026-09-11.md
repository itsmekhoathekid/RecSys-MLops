# Coordinator prompt baseline v30 and Qwen2.5 preflight

Date: 2026-09-11

## Result

- The prompt-only baseline migration was prepared by
  `RecSys-Workflow-Coordinator-Prompt-Baseline #2` and activated by
  `RecSys-Workflow-Baseline-Cutover #17`.
- Active workflow release:
  `ecbc567bdb4fbe94b0b9c8991747117e9111c19c1d67429c0428a6ee16d5123c`.
- Previous workflow release:
  `2458bfb95a8e3310adeea29ab39fe2f5ebf963d4f48060356fdc8c8fb84385c6`.
- Frozen control/candidate prompt checksum:
  `f2f003cb282da792b6e6cc189c2b202e740384fd058f337c215ef7bdff8f1ccd`.
- The migration changed only the Coordinator prompt. Generation config, LLM,
  specialist prompts, tools, bindings, runtime and revision marker were held
  fixed and verified by reproducible migration audit at cutover.

## Baseline preflight

The following three create-only infrastructure probes passed once on the new
control baseline. They are neither offline cases nor online A/B cases.

| Probe | Verdict |
|---|---|
| `prompt-baseline-coordinator-recommendation-a2a-v30` | PASS |
| `prompt-baseline-coordinator-context-a2a-v30` | PASS |
| `prompt-baseline-coordinator-composite-a2a-v30` | PASS |

## Candidate preflight

After an exact capacity preflight, the Coordinator-only Qwen2.5 candidate
`c7c0e73864510ea1c46785212dab10f259f632d384234d9be3d0bef16e5406d8`
was deployed at weight zero. `RecSys-Workflow-Coordinator-Candidate-Preflight
#3` ran the three v30 probes exactly once:

| Probe | Result |
|---|---|
| Recommendation | FAIL: no-retry `ReadTimeout` |
| Context | FAIL: `TASK_STATE_INPUT_REQUIRED`; eight Context A2A calls and eight responses |
| Composite | FAIL: no-retry `ReadTimeout` |

The result demonstrates that the stock ADK/model combination still re-enters
the same A2A function after a completed function response. The stronger prompt
state machine is not a sufficient enforcement boundary for this model.

No A/B experiment was created. Canary was not started, dispatch remained
disabled, and the online 20-case count remained zero.

## Capacity and cleanup

- `RecSys-Workflow-Prompt-AB-Adapter-Capacity #2` temporarily released one
  duplicate historical adapter on each node without reducing resource requests
  or the 200m safety margin.
- `RecSys-Workflow-Failed-Prompt-Candidate-Quarantine #1` scaled both candidate
  adapters to zero and removed its Coordinator SandboxAgent. Candidate model,
  specialists, backend and immutable MinIO evidence were retained.
- `RecSys-Workflow-Prompt-AB-Adapter-Capacity #3` restored both temporary
  duplicate adapters and verified their endpoints.

Final production state is `IDLE`, activated, with no experiment ID. Istio sends
100% of new workflow sessions to release `ecbc567b...`; the failed candidate is
absent from the route, its adapters are zero, Qwen2.5 backend is Ready, and
`AB_DISPATCH_ENABLED=false`.

## Repository verification

- Focused prompt/cutover/wiring tests: 81 passed.
- Jenkins/serving contract verification: 388 passed, one dependency warning.
- Graphify was updated after the implementation.

The repository-layout-only check remains independently blocked by pre-existing
untracked Dockerfiles under the router and an evidence directory; it is not a
failure introduced by this migration.
