# Workflow A/B implementation checkpoint — 2026-09-06

Status: **PARTIAL IMPLEMENTATION; NOT PRODUCTION ACCEPTED**.
The isolated workflow router/gateway chart is deployed with dispatch disabled.
The immutable baseline state/catalog and scoped credentials exist; the three-agent
release activation and UI cutover have **not** run. No workflow experiment has been
dispatched and no workflow promotion is claimed. Existing Recommendation A/B history
and the Final ML dashboard are preserved. Source changes remain local/uncommitted.

## Implemented in source

- Immutable three-role workflow identity, global defaults → fixed overrides,
  role-template rendering, mode validation, effective diff and NOOP rejection.
- Separate Istio gateway/router/adapter resources and three pinned ModelConfigs /
  SandboxAgents. New LLM identity uses a dedicated backend; config-only reuses it.
- Authenticated Langfuse inbox, durable event/version deduplication, finite dispatch
  Job and recovery CronJob, Jenkins dispatch reconciliation, immutable catalog lookup.
- Workflow Jenkinsfile; twenty root fixtures; bounded, authenticated live-test load
  Job manifest; zero post-promotion monitor policy; audited baseline restoration.
- Fail-closed child evidence collector using vendored kagent protocol definitions
  from `e6df917e9fa8`. The runtime supplies `subagent_session_id`; the collector calls
  only GetSession/ListTasks with the root user's identity. It never replays a child.
- DB snapshot metrics, source separation, distinct root/session counts, HA-dedup
  queries, redacted structured event projection, and nine dashboard sections.
- Dashboard UID remains `recsys-llm-ab`. Its JSON is generated from
  `jenkins/python/llm_agent_cd/dashboard.py` and provisioned by existing Helm glob.
  `model-ab-testing.json` has not been changed.

## Continuation r2 — additional implementation

- Workflow session, request and synthetic-case storage keys are now scoped away
  from the legacy Recommendation keys. Task polling filters by scoped owner before
  selecting a result, including when backend task IDs collide. Legacy keys remain
  compatible; workflow had no activated traffic to migrate.
- Additive trigger migrations persist the full authenticated webhook receipt and
  exact prompt snapshot before ACK. Concurrent deliveries deduplicate; conflicting
  content for a version is rejected. Body size is bounded while streaming.
- Kubernetes Job 409 is accepted only after checking the existing Job's managed
  spec. Recovery recreates the deterministic missing Job before dispatching an
  ACKed request; the recovery CronJob has the same restricted Job-creation role.
- Jenkins reconciliation ignores other jobs' queue entries and rejects multiple
  deliveries. A lost submission response never causes a second POST. Historical
  terminal results can be resolved through the immutable archive reference.
- Before the next workflow starts, its previous terminal snapshot is archived
  create-only under `workflow/experiments/<id>/<checksum>.json`. Integrity is
  checked on reads; archive failure blocks starting the next experiment. The next
  run clears stale gate/build/promotion/timeline fields. Closed CANARY/AB/VERIFY
  gate windows are retained separately from the latest rolling observation.
- Authenticated `GET /internal/experiments/{id}/evidence` exposes redacted current
  or archived evidence. Router restarts re-project historical structured events
  to Loki (at most five archives per heartbeat), without monitoring or inference.
  This does not yet prove historical Grafana rendering or all historical metrics.
- Receiver `/metrics` exports durable status, queue age and update timestamps as
  HA-deduplicated gauges. Dashboard panel 148 and Prometheus provisioning consume
  these metrics; existing UID and all panel IDs are preserved.
- Candidate scheduling preflight accounts for existing resource requests,
  replicas, init/sidecar peaks, overhead, selectors, taints and node pressure.
  Unsupported placement constraints fail closed. It is not a reservation;
  deployment readiness checks remain required.

### Q8 catalog published, model not started

The [same pinned ggml-org revision](https://huggingface.co/ggml-org/Qwen3.5-0.8B-GGUF/tree/8fea620810c4afa23dd6443f999a48574c1611a3)
contains Q4_0 and Q8_0. The downloaded Q8 artifact is **833,592,096 bytes**;
its actual SHA256 matches `37ae482d336108d23516fa35e8e0c4126688d81018b87178a18d752a1357814f`.
The create-only catalog was registered in production MinIO as LLM identity
`c1f26f480e21d804ca6f471e630be943faeac98a42b34d9a97761884677aa055`.
Serving image and settings match the Q4 baseline. No Q8 Deployment was created.
Source: `configs/llm-ab/catalog/qwen3.5-0.8b-q8_0.json`.

### r2 image and test result

- Built and pushed `recsys-llm-ab-router:workflow-20260906-r2` to the existing
  project registry, digest `sha256:d80e56467957ad0e0b33fb84e13b5c29ec52c17b291be2b39be20228c5eba45f`.
  Runtime import smoke passed in an isolated no-network linux/amd64 container.
  **This image has not been deployed**; production still runs r1.
- **212 unit/regression tests passed** (Jenkins + GKE postrenderer).
- **19 integration tests passed**, on disposable localhost PostgreSQL 16 only;
  the test container/database was removed afterwards. No production inference.
- Workflow chart lint passed with trigger enabled. Dashboard generator matches
  provisioned source JSON; no Grafana UI acceptance is claimed.
- All **54 PromQL queries** were accepted by production Prometheus's read-only
  query API; only two returned data for the checked baseline/live-test filters.
  The parser endpoint returned 404 on this server, so query execution was used.
  This is syntax/evaluation validation, not proof of chart completeness.
- Initial push was held by approval review; repository ownership, the existing
  production image path, Terraform project and image COPY payload were checked.
  The same push then passed review; no alternative destination was used.
- Live read-only Q8 capacity preflight returned HOLD: insufficient schedulable
  capacity for `rec-llm-c1f26f480e21d804ca6f`; no candidate deployed.
- Current Jenkins deployment has **0 desired replicas** (changed outside this
  continuation); the workflow facade pool is **1 replica**. Neither was resized.
  Capacity/quota still blocks full production acceptance. Baseline state remains
  IDLE, `activated=false`, and legacy Recommendation route remains 100% baseline.

Langfuse webhook shape and HMAC were checked against the
[official webhook documentation](https://langfuse.com/docs/prompt-management/features/webhooks-slack-integrations).
The version payload is fetched with the authenticated API; endpoints, credentials,
images and commands cannot be supplied from Langfuse config.

## Verification achieved

- Jenkins unit suite: 173 tests passed at the checkpoint.
- Continuation regression suite (Jenkins plus GKE postrenderer): **197 passed**,
  including twelve exact Envoy allocation tests. One Starlette deprecation warning.
- PostgreSQL integration: 8 tests passed on a disposable Docker PostgreSQL 16
  instance bound to localhost, not the production database. This includes SQL
  snapshot equality for two exporters and one sticky session spanning two phases.
- Workflow Helm chart lint passed with trigger enabled and a placeholder digest.
- Vendored gRPC schemas compile with pinned grpcio/grpcio-tools dependencies.
- 51 PromQL expressions accepted by the cluster Prometheus parser. This proves
  syntax only, not series availability, Grafana rendering or gate correctness.
- No production inference cases or load were generated during this checkpoint.

## Shared platform recovery completed in this continuation

The initial read-only checks found:

- `kagent-controller-5b6f48c779-mzj6z`: 0/1, CrashLoopBackOff.
- Both `ate-api-server-67948b4569-*` replicas: 0/1, CrashLoopBackOff.
- Substrate API error: `Failed to seed worker cache` / `CLUSTERDOWN The cluster is down`
  while reading Valkey cluster shard 0.
- kagent exits on timeout dialing the Substrate API; its Service advertises 8084,
  but port-forward confirms no process accepts that port in the controller pod.
- Router/gateway readiness alone therefore does not prove workflow serving health.

After explicit user authorization, existing six-node Valkey membership was
reconnected using current Pod IPs, preserving node identities, slot assignment and
persistent volumes. No RESET, FLUSH, reshard or key deletion was issued. Valkey
automatically elected node 2 instead of node 3 for one shard during reconnection;
this was not an operator-triggered failover. Key-count checks are evidence of
consistency, not proof of byte-for-byte absence of loss.

The StatefulSet now announces its Downward API Pod IP and gates readiness on
cluster health; the headless Service publishes not-ready addresses for discovery.
Both settings are persisted in the GKE mTLS postrenderer. All six pods rolled
successfully, reporting 16,384 healthy slots. Substrate API recovered 2/2 and
kagent controller 1/1 after stateless restarts. See the before/after metadata in
`docs/ops/evidence/valkey-recovery-2026-09-06-{before,after}.json`.

One infrastructure-only Coordinator request was executed during the Valkey roll.
Although its root task completed, a child failed DNS resolution; this is **FAIL**,
not a workflow PASS. Request `855e5dec-3185-4de3-a075-a9e4658d2d36` was not replayed
and is not part of the twenty synthetic cases. The headless discovery fix followed.

## Workflow bootstrap and current capacity blocker

- Helm `recsys-workflow-ab` revision 1 was installed with `trigger.enabled=false`.
- Router image: `recsys-llm-ab-router@sha256:cb6dc7d4003c51a88cd1900f17ed0cf398a31b4fa1cf233617c639b402cfaff6`
  in the existing `asia-southeast1-docker.pkg.dev/recsys-mlops-506406/recsys` registry.
- Workflow baseline: `dd03dedca0bfc0c8693057bc360f9b454205da32de7bc38cab6036d95628fd4d`.
  All three fixed overrides are empty; decimal `0` versus `0.0` no longer creates
  a spurious override masking a global temperature change.
- Create-only MinIO workflow state is IDLE; Q4 and now Q8 catalogs are registered.
  Least-privilege runtime read-only / CD writer accounts use only the `workflow/` prefix.
- Jenkins installation failed before contacting its API: its sole pod has been
  Pending for hours due to insufficient CPU requests. One workflow facade worker
  is also Pending. Router readiness alone is not end-to-end readiness.
- CPU autoscaler was temporarily restored to Terraform's 2/2 setting. GCE refused
  the additional node: `CPUS_ALL_REGIONS` limit 12, with the existing 8-vCPU CPU
  node plus 4-vCPU ML node consuming it. The autoscaler was restored to its live
  pre-operation 1/1 setting; no node was added or removed.
- The direct Jenkins job/credential installation has therefore **not succeeded**.
  No activation, traffic shift, inference suite, Langfuse dispatch or promotion ran.
- New source-only checks validate actual active Envoy destination/weight values
  (including forbidden retry/mirroring), not just route revision names. These and
  the updated job install helper are not in the already-deployed r1 image.

Capacity beyond the current 12-vCPU project quota is required before Jenkins and
the complete workflow can be safely tested. Do not reduce requests, replicas or
acceptance gates to conceal this blocker. Retain all baseline routes and data.

## Remaining work — mandatory before enabling dispatch

1. Obtain sufficient project CPU quota/capacity and recover Jenkins scheduling;
   verify all workflow facade workers Ready before activating the baseline.
2. Deploy and exercise cross-pipeline exclusion: the workflow job now holds the
   common lock for the whole run, plus source-level peer-state guards for crash
   recovery and normal deploy. Legacy running images/SCM still need integration.
3. Validate capacity for all three pools and candidate inference backend, and audit
   workflow cutover/normal-deploy guards end to end.
4. Finish Jenkins job/credential and Langfuse automation (Q8 catalog is now ready).
   Build/deploy an updated digest including the final source hardening, keeping
   dispatch disabled until baseline activation and cutover are verified.
5. Exercise real child-task retrieval and final assertions. Verify composite
   arguments, missing-user and empty-result cases against actual tool contracts.
   Organic trajectory/outcome handling and multi-turn evidence are currently
   fail-closed HOLD; they need a tested, turn-scoped collector before organic rollout.
6. Complete/validate per-agent latency, error classification, partial usage coverage,
   queue-age/readiness telemetry and run/source/time links. Missing fields must stay
   unavailable, not become zero. Do not infer per-agent latency from read duration.
7. Verify Loki delivery, table deduplication/20 rows, all filters, colors, units and
   trace links in Grafana. Historical charts after a subsequent experiment also
   need a tested archive projection; current-state export alone is not sufficient.
8. Finish controlled live fault-injection and production restart/CAS/capacity
   exercises. Concurrent webhook, uncertain Jenkins, scope isolation, archive and
   capacity source/integration tests now exist, but are not production acceptance.
9. Run the three real experiments sequentially with exact twenty synthetic roots
   each and bounded live-test policy. Restore the healthy baseline after the first
   two, run a separate controlled rollback, and retain successful combined champion.
10. Capture actual Langfuse receipt, Job/Jenkins, Envoy, dashboard and three-agent
    trace evidence; validate a normal deploy preserves pointers/weights/dashboard.

No screenshots, promotion, rollback exercise, 60-case result or statistical
superiority is fabricated or claimed. Post-promotion monitoring remains disabled.

## Local reproduction

```sh
.venv/bin/python -m apps.agentic.llm_ab_router.compile_protocol
.venv/bin/pytest tests/unit/jenkins -q
LLM_AB_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:15436/workflow_ab_test \
  .venv/bin/pytest tests/integration/test_llm_ab_router.py -q
```

The integration database must be disposable: fixtures truncate their test schema.
Do not point this command at production or a port-forward to production.
