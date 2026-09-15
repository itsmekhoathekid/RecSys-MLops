# Qwen2.5 terminal-template production preflight — 2026-09-11

## Outcome

The production `llm_only` experiment was not started. The immutable Qwen2.5
candidate failed the zero-weight full-A2A compatibility gate, so there was no
canary, no offline 3×2 suite, no online 20-response suite, no promotion and no
post-promotion monitor.

Production continued to allocate 100% of new sessions to workflow release:

`2458bfb95a8e3310adeea29ab39fe2f5ebf963d4f48060356fdc8c8fb84385c6`

The baseline Coordinator has exactly the Context and Recommendation A2A agent
tools. It has no direct MCP tools. The workflow uses stock Go ADK; no custom Go
ADK guard or execution-policy patch is present. The separately retained Go
patch propagates authenticated TaskStore user identity/ownership only.

## Capacity evidence

Jenkins `RecSys-Workflow-Terminal-Candidate-Capacity` build 6 passed under the
shared production and workflow-state locks.

- Candidate release: `d406faebb76eff219b3995107c6975e57ea337a9b60a709da4e40a4e842acc9e`
- Candidate LLM version: `5f8c2912e51a1ad9ead09ba37f57aae046919dda389e2f619c6a15fe37549f63`
- Journal: `workflow/capacity-windows/terminal-candidate-2458bfb95a8e3310adeea29ab39fe2f5ebf963d4f48060356fdc8c8fb84385c6-6.json`
- Exact Workflow-CD capacity preflight: PASS
- State and route: unchanged
- Inference requests: 0

The window pinned the three stock agent ScaledObjects to one replica, reduced
only the reviewed CPU request fields for Langfuse and core KEDA, and scaled
three zero-session historical adapters to zero. Three KEDA HTTP add-on
deployments were paused only after proving there were zero HTTPScaledObject
consumers. Memory, limits, PVCs and data were retained.

Earlier failed capacity builds restored their changes. Build 5 quantified the
remaining safety-margin deficit as 62m CPU (`138m` remaining versus the
required `200m`). Pausing the unused HTTP add-on released 75m without lowering
the model profile or the safety gate.

## Compatibility evidence

Jenkins `RecSys-Workflow-Candidate-A2A-Preflight` build 4 deployed the candidate
at Istio weight zero and issued exactly three create-only root requests. There
were no retries.

| Probe | Result | Evidence |
|---|---|---|
| Recommendation | FAIL | `ReadTimeout`; the stock Coordinator task continued to call after returned A2A data |
| Context | FAIL | rejected before execution because the sole Coordinator worker was occupied |
| Composite | FAIL | rejected before execution because the sole Coordinator worker was occupied |

The first task was observed quiescent after quarantine with 28 native function
calls and 28 function responses while still in `TASK_STATE_WORKING`. This shows
that the immutable `chat_template_tool_use` terminal instruction did not make
Qwen2.5-0.5B stop after the returned specialist result under the stock ADK and
llama.cpp b8646 flow.

These three requests are infrastructure compatibility probes. Their evidence
records explicitly contain `offline_requests=0` and `synthetic_requests=0`.
They must not be counted as the offline 3×2 or online 20-response evaluation.

## Quarantine and fail-safe state

Jenkins `RecSys-Workflow-Failed-Terminal-Candidate-Quarantine` build 1 passed.

- Candidate adapters were scaled to zero.
- The candidate Coordinator SandboxAgent was removed so its actor could not
  keep executing.
- Candidate ModelConfig, specialists, backend, TaskStore history and MinIO
  evidence were retained.
- Candidate never entered workflow state or Istio allocation.
- Baseline state and route remained unchanged.
- Quarantine inference requests: 0.

Jenkins `RecSys-Workflow-Dispatch-Prepare` build 14 then set
`AB_DISPATCH_ENABLED=false`. The webhook/status receiver remains Ready, but it
cannot submit Workflow-CD while the compatibility failure is unresolved.

Airflow, DataHub and MLflow tracking remain paused. Previously paused
Kubeflow/KServe control-plane components remain intentionally down; their data
and shared serving backends are retained. There is no automatic monitoring or
rollback loop after promotion because no promotion occurred and the approved
workflow does not include a post-promotion monitor.
