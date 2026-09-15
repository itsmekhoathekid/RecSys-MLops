# Small-model workflow implementation checkpoint — 2026-09-09

Status: PARTIAL IMPLEMENTATION. No production deployment or A/B acceptance run
was performed in this checkpoint. This is not a full-flow PASS.

## Implemented

- Operator-owned `qwen25-small-cpu-v1` profile: 1 CPU / 1536Mi requests,
  2 CPU / 2Gi limits; explicit non-thinking llama.cpp arguments with `--jinja`.
  No reasoning-budget arguments. Fixed context 16384, parallel 1, threads 2.
- Unknown profiles, changed profile settings, and wrong artifact checksums are
  rejected. Legacy catalog objects are not normalized or rehashed; their backend
  resource/argument defaults remain unchanged.
- Catalog registration permits the explicit small profile while retaining the
  pinned-image check and legacy quantization-only serving-settings restriction.
- Snapshot now requires live global ModelConfig defaults, not old Recommendation
  generation. Default agents are inspected individually; overrides are resolved
  against those defaults. Captured prompt revision markers remain unchanged.
- CLI snapshot compares default Recommendation prompt/tools and generation with
  the active champion, allowing only a terminal generated revision marker to
  differ. It rechecks object UID/resourceVersion, global Helm revision/status,
  and Recommendation state ETag before create-only local output and provenance.

## Verified evidence

- Downloaded official Qwen2.5-0.5B-Instruct Q4_K_M artifact: 491400032 bytes.
  SHA256 `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`.
  Local artifact: `.llm-agent-cd/qwen2.5-0.5b-instruct-q4_k_m.gguf`.
- Live global defaults: temperature 0.0, maxTokens 384, seed 42; all role
  overrides empty. Default Recommendation prompt/tools match the champion after
  terminal revision-marker normalization.
- New local baseline `85d74972a5538e3754cb66b062f94fc732c50a6425fdf379d48a050eb6b17cdc`:
  `.llm-agent-cd/workflow-global-20260909.json`, with adjacent provenance JSON.
  Existing MinIO champion/state and historical manifests were NOT replaced.
- Built local linux/amd64 image `recsys-llm-ab-router:workflow-small-20260909-r3`.
  OCI index digest `sha256:46b53dc59fa86ee7413301d17013d4919e4a2bd7b079c67fcf65188939cafcb4`.
  Image import/profile smoke passed with networking disabled. Image not pushed.
- Dedicated profile/snapshot tests: 8 passed. Broader Jenkins/Substrate regression
  suite: 223 passed (one existing Starlette deprecation warning). No compatibility
  inference yet.

## Still required before production dispatch

1. Finish global-deploy Jenkins wrapper/job and common-lock enforcement. The
   current direct Helm script has NOT been converted and must not be treated as
   safe for concurrent execution with workflow A/B.
2. Complete active Coordinator/router graph inventory and readiness checks,
   snapshot race tests, and inactive-baseline state reconciliation. Local baseline
   output alone does not authorize overwriting the existing workflow state.
3. Complete turn-scoped collector, dashboard global-source/profile projection,
   historical evidence validation, webhook/job wiring and end-to-end lock tests.
4. Build/push the final completed runtime, then open the approved capacity window;
   perform placement/surge preflight and all readiness checks before activation.
5. Run six separate compatibility checks, then config-only and llm-only in order,
   exactly 20 synthetic roots per experiment, unchanged gates and no top-up.
6. Capture real promotion and controlled rollback evidence. No periodic monitor
   after promotion. Offline services, once intentionally stopped for this run,
   stay stopped until the user asks to restore them; retain PVCs/shared backends.

No live-test load, synthetic conversations, promotion, rollback, or memory/latency
benchmark of the small model has occurred. The RAM estimate is not a measurement.

## Subsequent GCP deployment and compatibility gate

This section supersedes the earlier checkpoint status above.

- Airflow scheduler/webserver/private PostgreSQL, DataHub GMS/private MySQL/
  OpenSearch, and MLflow tracking were stopped with retained PVCs. No active
  Airflow tasks were present. They remain stopped until the user asks to restore.
- Applied the approved CPU request reductions to all three agent pools, EPP,
  ClickHouse and Triton. Each rollout completed; replicas, memory and limits were
  retained. Jenkins moved to E2 and is Ready 1/1 with its existing PVC.
- Runtime r4 pushed to the existing RecSys Artifact Registry and deployed through
  Helm `recsys-workflow-ab` revision 2. Router Ready 2/2, facade remains 1 while
  activation is incomplete; trigger remains disabled.
  Digest: `sha256:918bcd199c8c89f0ab2c667d065506a00cd6178d85052d4511ae26de8ec13803`.
- Installed `RecSys-LLM-Workflow-CD` and `RecSys-Global-Model-Config`; both buildable,
  next build number 1 at verification. Global CLI now queues Jenkins rather than
  applying Helm directly; job defines the common release lock and phase guard.
  Live lock-contention/fault testing is not yet completed.
- Registered small catalog in MinIO:
  `2c87bcf043928361e3fc16b5293dd1abaf41679eb87a4af2a03f2f6264e99c8e`.
- Remaining-stack requests placement preflight and backend server dry-run passed.
  Created the isolated model backend; init checksum verification and readiness
  passed on GCP. No workflow route pointed to this backend.

### Actual compatibility result: FAIL, 3/6

Exactly six direct HTTP inference calls, no retries, no real tools, zero synthetic
A/B cases. Tool selection, arguments and mocked composite next step passed.

- Tool result: returned prose instead of required JSON.
- Missing user ID: generated a tool call with invented `user_id=123`.
- Empty result: returned an apology/request for information instead of the empty
  fixture result.

Evidence: `evidence/small-model-compatibility-2026-09-09.json`; individual raw
fixture responses in `.llm-agent-cd/compat-small-20260909-run1/`.
Observed pod memory samples were 282Mi before and 322Mi after the run; these are
samples, NOT peak RSS or a sustained-load benchmark.

Backend `rec-llm-2c87bcf043928361e3fc` was scaled to zero after FAIL. Manifest and
Service remain for audit, with no backend endpoints. Catalog is published but
NOT accepted for rollout; do not submit it as a production challenger without a
new approved compatibility attempt. Champion and Istio Recommendation route
unchanged; no workflow activation, promotion, or 20-case experiment occurred.

Collector multi-turn/organic evidence, full dashboard validation and webhook
automation remain unfinished. Dispatch was deliberately NOT enabled; enabling it
is not an acceptable substitute for fixing the failed compatibility gate.
Next work requires revising candidate selection or the approved compatibility
approach; do not change business prompts or weaken assertions to force PASS.

Final health checks: three agent pools 2/2, workflow router 2/2, Jenkins 1/1,
Recommendation/RAG APIs 1/1 and ClickHouse 1/1. Regression suite: 229 passed.
