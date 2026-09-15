# Production bootstrap — 2026-09-05

Status: **PRODUCTION CUTOVER VERIFIED — 100% existing champion, ready for user live testing.** No 20-case evaluation, challenger promotion, controlled challenger rollback or 24-hour monitoring acceptance has run. Experiment state remains `IDLE`; absence of A/B evidence remains `HOLD`, not PASS.

## Completed after explicit CRD approval

- The user approved the shared CRD compatibility change. Missing BYO fields were added to the old schema; storage remains `v1alpha2` and existing fields were preserved. API readback and controller readiness now confirm the BYO command survives storage.
- Helm `recsys-llm-ab` is revision **5**, and Coordinator is revision **64**. The old Coordinator revision **63** is retained for emergency fallback. All five SandboxAgents are `Accepted=True`, `Ready=True`.
- Current router/control image: `asia-southeast1-docker.pkg.dev/recsys-mlops-506406/recsys/recsys-llm-ab-router@sha256:e9b8393bb76a2c3777893147dfb8982a3a08c8a0b92ad5f402c101f73e70009c`.
- The champion adapter stays pinned to its original image `sha256:eaae1788b8c2b9ce4ea05312caae1f44ac7102e1e15cb6875a1b1637f34663c2` via `binding.adapter_image`. This binding was recorded with a conditional state write after matching the actual adapter spec/annotation; config, LLM and release IDs did not change.
- `VirtualService/recsys-ab` routes allocations 100% to `rec-ab-7c965ae4a090d308fb5d`. Both Envoy configuration dumps and an authenticated `/identity` request verified route revision `441936e7cef517c9ba1119ad709a048f5efe1ccd02c92c94882d069c8addca9a`.
- Jenkins `RecSys-LLM-Agent-AB-Activate` build **5: SUCCESS**. `RecSys-LLM-Agent-AB-Cutover` build **1: SUCCESS**. Cutover held the production and Coordinator Helm locks, preserved live values, archived the old revision, and armed automatic Helm rollback until its smoke passed.
- Coordinator's actual tool reference is now `recsys-recommendation-router`. Its separate infrastructure smoke `ce66e932-e12b-4a28-8d0c-de0a2d3f02b2` completed in ~65 seconds and called `kagent__NS__recsys_recommendation_router` exactly once. Direct-facade smoke `e334a6a1-a62f-4160-b4f8-33b4dba06118` completed in ~28 seconds. Neither request was retried or counted toward the 20-case suite.
- Live evidence exposed that this kagent version puts function events and usage in task **artifacts**, not only history. The parser now handles both forms, deduplicates mirrored events and aggregates per-invocation token usage. The direct smoke payload passes strict argument/ranking/metadata comparison when inspected by the corrected parser. Its original stored HOLD result is retained as historical evidence. The Coordinator smoke's free-text child response remains HOLD under the strict JSON acceptance gate; functional routing success is not a claim of A/B acceptance.
- Worker pool labels now match the actual ActorTemplate selector. The two idle facade workers were recreated individually to refresh registration; existing specialist pools were not restarted. No new node or LLM backend was created.
- Final regression result: **153 Jenkins unit tests passed**, Helm lint and whitespace checks passed. Source changes remain local/uncommitted; merge them before expecting future SCM-driven deployments to retain the new activation guard. Installed A/B jobs currently use the deployed-image source mode. The installed Jenkins definition also rejects rollback when pending equals baseline, preventing quarantine of the sole champion before an experiment. The equivalent engine-level defense is in local source for the next image build; the deployed job-level defense is already active.

For user testing, use the existing [Agent UI](https://agents.recsys-mlops.site) with the Coordinator and a **new session**, then inspect the [A/B dashboard](https://metrics.recsys-mlops.site/d/recsys-llm-ab/llm-agent-a-b-rollout). Starting an actual A/B run still requires a reviewed candidate manifest and live evidence at every gate. Use the current champion manifest in MinIO, including its adapter image pin, when constructing a `config_only` candidate.

Emergency pre-A/B cutover fallback (operator action, no request replay): acquire the same Jenkins deployment locks, disable `recsys-ab-activation`, roll Coordinator back to Helm revision 63, and verify readiness/tool reference. Do not delete the shared LLM backend or versioned state. Once experiments begin, use the CD `rollback` action to restore the recorded composite release rather than substituting only config or model.

## Historical bootstrap notes before approval

### Initial deployment

- Cluster: `gke_recsys-mlops-506406_asia-southeast1-b_recsys-mlops-gke`.
- Helm `recsys-llm-ab`, namespace `kagent`, revision 2: router 2/2, private gateway 2/2, dedicated facade WorkerPool 2/2. Lightweight HTTP components use existing ML-system capacity; no node or LLM backend was added.
- Runtime image: `asia-southeast1-docker.pkg.dev/recsys-mlops-506406/recsys/recsys-llm-ab-router@sha256:7c42caecdf3bc9c7fae633c887c8e6682a60f43fa5d3ac8d7bb87a591690747c`.
- Dedicated PostgreSQL `recsys_ab` database and role, separate from kagent tables.
- Versioned MinIO bucket `recsys-llm-ab`, scoped runtime-read and CD-write accounts. State: `s3://recsys-llm-ab/recommendation/state.json`; currently `IDLE`, gate `HOLD`.
- Original model's loaded GGUF SHA256: `57d1997790d1744fba5b40a7317df71ea5e2acee28c47e78f0cce39c0703f8cf`. Verified from the existing pod's actual HF snapshot file, not a mutable alias. The release preserves its gateway headers and reasoning-budget message.
- Champion release: `7c965ae4a090d308fb5d030a97c00e5d6af55a162fc8f94248e7ad7295732f4c`; manifest also stored at `s3://recsys-llm-ab/releases/<release_id>.json`.
- Jenkins jobs `RecSys-LLM-Agent-CD` and `RecSys-LLM-Agent-AB-Activate` installed with scoped Secret-file credential. Their corrected definitions pass Jenkins' non-executing pipeline validator. They extract source from the digest-pinned deployed image because local source is not yet merged/pushed.
- [Live Grafana dashboard](https://metrics.recsys-mlops.site/d/recsys-llm-ab/llm-agent-a-b-rollout) returns HTTP 200 through the authenticated Grafana API. Grafana/OTel rollouts completed; Prometheus reload returned HTTP 200. Router `/healthz` and `/metrics` return HTTP 200.
- Only A/B additions were patched into live observability resources. Unrelated differences in the local chart, node placement and existing scrape jobs were preserved.
- Existing Coordinator and Recommendation SandboxAgents remain `Accepted=True`, `Ready=True`; Coordinator still references `recsys-recommendation-agent-sandbox`.

### Original blocker (resolved after approval)

`sandboxagents.kagent.dev` has `v1alpha2` as storage version and `conversion.strategy=None`. Its v1alpha3 schema exposes BYO `image`, `cmd`, `args`, `env`, but the storage schema lacks these fields. Actual API readback shows `spec.byo: {}` after apply; the pinned controller rejects the new facade because `spec.byo.cmd` is missing. Merely passing a server-side dry-run is insufficient to detect this pruning.

The production safety reviewer rejected changing the shared CRD. **No CRD modification was executed.** The prepared `provision byo-schema` command only adds missing BYO fields from the existing v1alpha3 schema, retains old properties and leaves the storage version unchanged, but must not be run until this shared-CRD change is explicitly approved. Version conversion background: [Kubernetes CRD versioning](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definition-versioning/).

The first activation build failed at Jenkins compilation because `timestamps()` was unavailable; that option is now removed and both definitions validate. It never deployed the immutable adapter or created `VirtualService/recsys-ab`. No activation marker or Coordinator cutover was performed.

### Original resume checklist (bootstrap/cutover now completed)

1. Review the additive CRD patch, apply it only with approval, then reapply the A/B chart and require a persisted BYO command plus `Accepted/Ready` conditions. Keep the facade pool separate from Recommendation workers to prevent parent/child worker exhaustion.
2. Build a fresh image from current source: the latest fail-fast storage-schema guard is tested locally but is **not** in the deployed image above. Pin the new digest consistently in the chart and Jenkins jobs.
3. Run the locked activation job. Require exact immutable agent/backend readiness and verified Envoy route at 100% existing champion before proceeding.
4. Preserve live Coordinator Helm values; apply `configs/llm-ab/coordinator-router.yaml` only after activation. Verify the new golden snapshot and A2A chain; keep revision 63/current pre-cutover revision available for fallback.
5. Validate fixture data and perform the separate infrastructure/live smoke. Only then start an experiment; do not top up or repeat its fixed 20 requests, and do not interpret insufficient evidence as PASS.
6. Capture actual routing, case results, traces, promotion and controlled rollback evidence. Current `IDLE/HOLD` dashboard is not proof of completed A/B acceptance.

Local verification after production fixes: all 149 Jenkins unit tests passed, including six new schema/migration regression tests; Helm lint passed; `git diff --check` passed. Local changes remain uncommitted. The unrelated `docs/pngs/observe_llm_1.png` was not modified.
