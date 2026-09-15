# Workflow evaluation production checkpoint — 2026-09-09

## Latest checkpoint — 2026-09-10 local: guarded recovery, still NOT ACCEPTED

Full A/B acceptance is **blocked**, not completed. The newest production run is
`wf-418d215f6a8438d05d4597fa71fc8c4a` (Langfuse version 2, Workflow-CD #4).
Its six compatibility checks passed and scores were read back from Langfuse.
Envoy reached 10%; three separate live-test roots failed (two control, one
candidate), so the pipeline automatically verified rollback to 0% candidate.
**Synthetic remains 0/20, NOT_STARTED.** No 50%, 100%, promotion, or separate
controlled-fault acceptance has occurred.

Latest [machine-readable evidence](evidence/workflow-evaluation-followup-2026-09-10.json)
supersedes the current-state assertions in the historical sections below.

### Completed recovery and diagnosis

- Restored the missing global recommendation index from **194 real historical
  product feature records**, not fabricated IDs. Candidate-Recovery #3 succeeded;
  its create-only MinIO snapshot records the source timestamp/checksum. The
  fixture v2 now uses verified real user IDs, with separate provenance; v1 and
  failed-run history remain intact.
- Serving-Repair #2 deployed the image-only Online Feature API fix. A real valid
  product is retained; an unknown product is excluded instead of being ranked
  from an empty feature vector. Shared backends and PVCs remain intact.
- Evaluation-Prepare #9 deployed transport-attached child-task evidence and the
  current collector/router tooling. Successful full three-agent collection is
  **not yet proven**: the canary stopped on runtime violations before that point.
- All three failed canary traces are present on Langfuse. Use bounded
  `/api/public/v2/observations` reads on v4; the deprecated v1 endpoint's 404 did
  not imply trace loss. See [Langfuse's supported observations API](https://langfuse.com/docs/api-and-data-platform/features/observations-api).
- Candidate had preserved user ID `218`, but guard parsing rejected unquoted
  `user_id:218`. The guard now recognizes that syntax, preserves conflicting-ID
  rejection, and distinguishes a blocked attempt from an executed tool failure.
- The pinned llama.cpp server (`b10380-0b1bad14f`) logged that object-form
  `tool_choice` was unsupported and downgraded to its default. Runtime r6 uses
  string `required` only when exactly the selected tool is exposed, retaining
  argument checks, sequential execution, and no replay.

### Real serving check still fails — no cutover

Cloud Build `927fc285-fac5-4733-9a6a-9c4acee0fb4a` built runtime digest
`e19bbb84b9944b32a724e6cd25a6bc04fcf8ff6a4c461bf0c4f0cdbd87c89892`.
Baseline-Prepare #6 created immutable release
`3d31ce1dbe53ad3b9079c615ffd493935d6121861b32bc944949e92a1599667e`;
three missing-user probes passed without model/tool execution.

Before cutover, #7 issued **one additional read-only recommendation serving
probe**, separately labeled `infrastructure_test`, not offline or synthetic A/B.
It failed: the control model repeated argument JSON and closing `</think>` tags
until **384 output tokens**, with `MAX_TOKENS` and **zero tool executions**.
Trace: `c6b6a4ed2be5c73655df716fbffb12ee`. The probe result is create-only; it was
not replayed. This is not evidence that simply increasing the token cap fixes
the serving/template interaction. Changing the frozen prompt, generation config,
or shared control serving identity requires a reviewed baseline-plan revision.

**Prepared release 3d31… was not activated.** Cutover source now rejects a later
failed/unresolved serving probe even if an older preparation object is selected.

Jenkins #7 exposed a stale-workspace artifact bug: it archived #6's success JSON
despite its own FAILURE. Treat that artifact as invalid, not as probe PASS.
Preparation jobs now reset the artifact per build and write a current-build
failure record. Negative check **#8** reread the failed probe without inference
and produced its own `PREPARATION_FAILED`, build number `8`. Old artifacts were
not rewritten; this correction is additive evidence.

### Capacity, current state, and continuation

- Capacity-Prepare #1/#2 journaled adapter retirement and moved the unchanged
  telemetry probe placement to E2. Only unused/quarantined adapter pods were
  stopped; release objects, sessions, models and backends were retained. The
  current failed candidate has 0 adapter replicas and cannot receive new turns.
- #2 passed **baseline-repair-only** placement, not full A/B capacity. With a
  fresh candidate pair, projected E2 CPU request headroom is **181m**, below the
  **200m** policy floor; N2 memory request headroom is about **220.55 MiB**. The
  capacity gate remains HOLD. No quota increase or additional request/limit cut
  was made. A placement/capacity revision is still needed independently of the
  serving compatibility repair.
- Champion remains `924e9b8b84915b6fd2e68e61309481f5de7c9aa173de7c5dae2096ac6fdcc8c1`;
  previous remains `e10b72a49e62723eaab393eadd7775cd4724bbcf4fd29028a1e93ce8fd8146a4`.
  State remains ROLLED_BACK, candidate weight 0. Langfuse automation is INACTIVE;
  receiver/recovery/evaluation services remain available. No monitor gate exists.
- Jenkins ACL recheck passed: existing admin retained; dispatch can read/build
  Workflow-CD only. Configure/script access returns 403, the unrelated job 404.
- Airflow apps/PostgreSQL, DataHub apps/MySQL/OpenSearch and MLflow remain paused.
  Keep them paused until explicitly requested. Do not resume with an old
  Langfuse version or alter failed evidence; the next experiment needs a fresh
  verified baseline and candidate version.

Verification: **316 A/B unit/disposable-PostgreSQL integration tests**, **26
serving tests**, and Go agent/model/tool tests passed. Scripted regressions cover
missing user, empty results, metadata preservation, exact outgoing tool-choice
serialization, concurrent/restart claims, and blocked attempts without replay.
Browser/dashboard screenshots, full 20-case quality coverage, successful
promotion, actual Jenkins restart, and normal-deploy preservation acceptance
remain outstanding. No superiority claim is made.

## Historical checkpoint: first trigger, canary failure and rollback

**NOT ACCEPTED / ROLLED_BACK.** The approved Jenkins authorization migration
and real Langfuse → Kubernetes Job → Jenkins dispatch are working. This does
not establish a successful 20-case A/B or promotion.

Current proof: [dispatch, experiment and rollback evidence](evidence/workflow-evaluation-jenkins-run-2026-09-09.json).

- Jenkins now uses Project Matrix Authorization (`matrix-auth` 3.3). Existing
  local account `admin` keeps its access. `recsys-workflow-dispatch` has global
  Read and **only Workflow-CD Read/Build**, not Configure/admin/other-job access.
  Effective ACL and authenticated HTTP checks passed. Startup source and the
  live init ConfigMap preserve the matrix strategy; an actual Jenkins restart
  has not been performed. The previous strategy is backed up on its PVC at
  `/var/jenkins_home/workflow-authorization-before-matrix.xml`.
- Baseline-Cutover #1 and Dispatch-Prepare #1/#2 succeeded under the shared
  release lock. Activated baseline is `e10b72a49e62723eaab393eadd7775cd4724bbcf4fd29028a1e93ce8fd8146a4`.
  No Helm-global default or legacy Recommendation route was changed.
- Langfuse prompt `recsys-workflow-ab` version 1 with `ab-ready` produced
  experiment `wf-79995644aa1ba3c8a80b48ed7cb81392`, deterministic dispatch Job,
  queue 226 and **Workflow-CD #1 started by the scoped dispatch account**.
- **Offline: 6/6 PASS**, 3 cases per model, with all six evaluation records
  confirmed in Langfuse. These are compatibility smoke checks, not full workflow
  quality tests. No extra offline inference was run during recovery/maintenance.
- Envoy verified **10%** candidate allocation. Canary's separate live-test load
  completed **20 roots: 16 FAIL, 2 HOLD, 2 PASS** (18 control, 2 candidate).
  **Synthetic: 0/20, NOT_STARTED.** No 50% A/B, 100% verification or promotion
  occurred. The 20 operational requests must not be counted as the requested
  20 synthetic quality cases.
- Real failures were child runtime errors (11), root runtime errors (3), and
  invalid operational trajectories (2). Two missing-user checks passed; two
  invocations lacked enough operational evidence. Recommendation's shared
  dependency returned 502: Online Feature API could connect to Redis, but
  `candidate:user:1001` and `candidate:popular:global` were both absent
  (ZCARD 0, TTL -2). A separate read-only infrastructure GET confirmed the same
  502. No fake candidate IDs were inserted and no fallback was enabled.
  **Missing data is not proven to explain every trajectory/guard failure.**
- A gate-ordering bug allowed insufficient candidate samples to mask known
  control failures as HOLD. #1 was stopped and rollback requested. #2 failed
  before rollback because an empty Jenkins candidate parameter was unset under
  `set -u`; shell defaults were fixed. #3 performed the rollback, although its
  Jenkins result is FAILURE because the CLI returns nonzero for ROLLED_BACK.
  The state, both Envoy gateways and baseline identity endpoint—not a green
  build badge—verify the successful rollback.
- Final phase **ROLLED_BACK**, candidate weight **0**, baseline unchanged and
  failed candidate `f9ae9556cd376832da28e06815d24036f2705dcdee5bd6611fbf6069f8e975a5`
  disabled. Load is stopped. Langfuse automation is **INACTIVE** again. Receiver
  and delivery/reconciliation CronJobs remain enabled; these do not implement
  post-promotion monitoring. Keep immutable releases/backends with references.
- Follow-up tooling r5 was built and deployed by **Evaluation-Prepare #8,
  SUCCESS**. Known errors now take precedence over insufficient samples, and
  recovery projects a manually rolled-back known build into dispatch status
  without redispatch. CronJob has actually reconciled the row to ROLLED_BACK.
  Router/receiver are 2/2 Ready. State ETag and champion are unchanged.
  #7 safely stopped before mutation because it reserved an unnecessary
  collector rollout. #8 used the actual unchanged collector diff while still
  reserving router/receiver surges and both finite Jobs: N2 443m/220.55 MiB,
  E2 291m/3577.14 MiB request headroom. No quota or resource reduction.

Local verification for this update: **146 unit tests, 18 disposable PostgreSQL
integration tests PASS**; JSON/diff checks and graph update completed. The local
test container was removed; no production data was deleted. The small model is
Ready and was observed at 331 MiB idle RSS/working-set reporting; that is **not a
peak-inference memory benchmark**.

### Blockers before a new acceptance run

1. Restore/materialize real recommendation candidate data through the owned ML
   data path, with shared-data scope reviewed. Airflow/DataHub/MLflow must stay
   paused unless the user explicitly requests otherwise. Do not seed fake IDs.
2. Diagnose and correct the remaining guarded trajectory/evidence failures;
   create a new immutable baseline/candidate revision if runtime or contract
   changes. Do not modify the failed experiment or replay its executions.
3. Run a new candidate version only after dependency/runtime checks pass; retain
   the failed history. The 6-call smoke/20-case suite, 10/50/100 gates and quality
   coverage policy are not weakened to obtain PASS.
4. Grafana browser verification/screenshots remain pending login. The dashboard
   is provisioned (UID unchanged) but 20/20, promotion and controlled-fault
   screenshots do not exist because those stages did not run. The observed
   incident rollback is not the separate planned fault-injection acceptance.

**Post-promotion monitoring remains disabled. No superiority claim is made.**

## Earlier preparation checkpoint (historical, superseded above)

Status: **PREPARED, NOT ACCEPTED**. This records actual production preparation,
not a completed A/B experiment. The authoritative DB has **0 offline rows and
0 workflow synthetic rows**; no 6-request smoke or 20-conversation acceptance
suite has run in this deployment. Unit/infrastructure probes are separate.

Machine-readable proof: [production preparation evidence](evidence/workflow-evaluation-production-preparation-2026-09-09.json).

## Deployed and verified

- `RecSys-Workflow-Evaluation-Prepare #6`: SUCCESS. Router/evaluator image r4,
  digest `67178a46049fac8d40aee68b942943c1d5d3b5c61b159067556dcdc08e4643de`,
  router 2/2 Ready. Additive DB schema, evaluation outbox and durable live-load
  budget claims are deployed. Evaluation CronJob exists but is suspended.
- `RecSys-Workflow-Runtime-Prepare #3`: SUCCESS. Controller supports opt-in
  guarded Go ADK image/policy without changing legacy agent runtime, shared
  ModelConfig or default agent specs. Controller stays at the capacity-window
  one replica. Helm/Terraform post-renderer prevents unintended replica reset.
- `RecSys-Workflow-Baseline-Prepare #4`: SUCCESS. New guarded baseline
  `e10b72a49e62723eaab393eadd7775cd4724bbcf4fd29028a1e93ce8fd8146a4` uses
  Go ADK r3 `af2e97bae5f6ac94fd93c479ecb788a461003277b04a84fddef32af0cd1f19d4`.
  All three agent ModelConfigs/SandboxAgents are Ready. Real missing-user
  probes passed for all three roles, with no tool calls and no observed LLM
  token usage. They are not offline/A/B cases.
- Official Qwen2.5-0.5B Q4_K_M artifact and v2 tool template were checksum-checked
  and catalog-published. LLM ID:
  `de1d2557a58d6e1114497dec8b77cf259bd6953ffb5bba27b3a9c395abd6c3ae`.
  Both paused small-model Deployments remain at **0 replicas**.
- Langfuse 4.17.0 real score round-trip succeeded for BOOLEAN, NUMERIC and
  CATEGORICAL values. In-cluster Job
  `recsys-workflow-evaluation-preflight-20260909` reports PASS, 0 inference.
- Langfuse automation `cmtu4klxn0003yl075sh7shev`, name
  **RecSys workflow A/B dispatch**, was created **INACTIVE**, filtered to exactly
  prompt `recsys-workflow-ab`. A second prepare read the same automation without
  duplication or secret rotation. HMAC is stored in owned Kubernetes Secret
  `kagent/recsys-workflow-webhook-auth`, never in this report.
- Grafana dashboard JSON and UID `recsys-llm-ab` were provisioned through the
  locked preparation build. Collector metadata allowlist is deployed. Real
  browser verification/screenshots still require login; provisioning is not
  proof of correct rendered charts.

## Important fixes and retained failures

1. r2 runtime did not parse `Recommend exactly up to 1/2` correctly. Fixed in
   r3, with regression tests. The r2 baseline/history remain unchanged and are
   **not** the baseline to activate.
2. Baseline prepare #3 failed because Coordinator was compiled before its
   referenced specialist existed. Publish specialists first; after both are
   Ready, a scoped label triggers recompile of that exact failed Coordinator.
   No spec/prompt change or inference replay. Prepare #4 succeeded.
3. Controller prepare #1 temporarily inherited chart's three replicas. The
   capacity-window one-replica value was restored by audited prepare #3 using
   a controller-only post-renderer. Do not use a plain chart upgrade that
   bypasses that post-renderer.
4. Live-load run ownership is create-only in PostgreSQL. A replacement Pod/Job
   cannot acquire a fresh budget. Individual submissions are counted durably
   before sending, capped at 360. Ambiguous owners are not resumed/replayed.
5. Paused v2 backend may resume only its replicas field after complete owner,
   immutable spec/template and image checks. ResourceVersion CAS prevents a
   stale patch. Drifting manifests are never overwritten.
6. New cutover implementation records a CAS intent before route application,
   verifies Envoy and the data plane before committing champion, and closes
   new workflow execution during an incomplete cutover. It is unit-tested but
   **not executed on production yet**. Do not run the older generic `activate`
   action against the still-active bootstrap pointer.
7. Cloud Build r4 upload was initially rejected by approval review. Read-only
   project/context/registry verification plus an exact gcloudignore manifest
   (192 source/config files, 978118 bytes; no credentials, state, user data or
   GGUF) resolved the rejection. Build `422a7951-cbeb-4c23-ac81-ae7ec404b76f`
   succeeded in the verified RecSys project.

## Capacity and retained infrastructure state

Full read-only preflight PASS for r3 baseline + small-model candidate, including
the second facade worker, receiver replicas, finite/recovery Jobs, candidate
adapters and retained r2/r3 adapters:

| Node | Forecast free CPU requests | Forecast free RAM requests | Current RAM available at check |
| --- | ---: | ---: | ---: |
| N2 | 443m | 220.55 MiB | 48.44% |
| E2 | 241m | 3449.14 MiB | 44.71% |

Both exceed the 200m/128Mi per-node floor, but the N2 memory margin is narrow.
This is not a scheduler reservation or a model memory benchmark. Recheck
immediately before placement and measure after startup. No quota increase or
additional rightsizing was performed.

Airflow, DataHub and MLflow remain paused as requested. PVCs/shared backends are
retained. Do not restore these automatically after a test or failure. Their
snapshot/recovery journal is [ab-capacity-window-2026-09-09.md](ab-capacity-window-2026-09-09.md).

## Why dispatch remains off

Live Jenkins authorization is
`hudson.security.FullControlOnceLoggedInAuthorizationStrategy`; none of the
matrix/role/build-token-root authorization plugins is installed. A new
"dispatch" user would therefore have **administrator-level access to all CI
jobs**, not just Workflow-CD. No Jenkins admin credential has been supplied to
the receiver.

Safest next decision: authorize a reviewed Jenkins job-scoped permissions
migration, preserving existing users' access and giving the dispatch service
only the required Workflow-CD read/build and queue visibility. This changes
shared CI authorization and is not silently bundled into an A/B rollout.
Keep the automation INACTIVE and receiver dispatch disabled until that decision
and effective-permission tests are complete. Do not bypass it with admin tokens.

## Remaining work before claiming full flow

- Apply and verify scoped Jenkins/MinIO dispatch credentials; deploy receiver,
  HTTPS webhook ingress and recovery CronJob initially disabled. Validate real
  HMAC receipt, replay protection, dedup and uncertain Jenkins delivery.
- Recheck provenance/capacity, activate the prepared r3 baseline via the new
  intent/CAS cutover, verify workflow BYO entrypoint on UI/API and all three
  agents' real invocation/trace binding. Finish turn-scoped child collection;
  ambiguous multi-turn evidence currently HOLDs rather than being guessed.
- Complete captured RAG-index provenance and full output-schema/evidence review.
  Verify Helm-global defaults vs effective/historical config dashboard data.
- Create the actual Langfuse candidate version from the **activated** champion,
  then label `ab-ready`; enable evaluation delivery and dispatch only after
  their readiness/permission checks. Do not silently rebase a stale version.
- Run exactly 6 offline compatibility calls, then the 10/50/100 rollout with
  exactly 20 synthetic conversations and separately budgeted live-test load.
  Missing evidence is HOLD; failure/timeout rolls back. No top-up or replay.
- Check Langfuse experiment item visibility, scores, DB counts, token totals,
  gate snapshots, traces, live Envoy weights and Grafana rendering. Capture real
  screenshots; no acceptance charts or success screenshots have been fabricated.
- Run controlled live-test-only fault rollback and normal-deploy preservation
  checks separately. Preserve all historical failures and existing sessions.

Verification completed locally: **254 Python unit tests and 25 PostgreSQL
integration tests PASS**, bounded Go runtime/guard tests PASS; `git diff --check`
and `graphify update .` succeeded. Local test mocks do not establish production
quality, compatibility, statistical superiority or successful promotion.

Workflow state remains IDLE/inactive, champion bootstrap
`dd03dedca0bfc0c8693057bc360f9b454205da32de7bc38cab6036d95628fd4d`, previous null,
experiment null. Legacy Recommendation serving route has not changed.
**Post-promotion monitoring remains disabled by design.**
