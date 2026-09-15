# Recommendation Agent A/B delivery

## What is implemented

Recommendation delivery uses three bounded components. The
`recsys-recommendation-ab-poller` CronJob is the only controller and evaluates
one durable state-machine tick each minute. `jenkins/LLMAgentCD.Jenkinsfile`
executes exactly one registered `prepare`, `route`, `promote`, `rollback`, or
`cleanup` action under the production lock. A separately started Traffic Job
emits public HTTPS traffic. There is no Jenkins resume loop and no periodic
post-promotion monitor.

Traffic calls the production public A2A endpoint, which verifies Basic Auth and
a one-shot signed ticket before forwarding to the private Recommendation
router. Coordinator and Context are neither invoked nor required by this
Recommendation-only run. The separate workflow job and state remain unchanged.

An immutable release binds generation configuration, model artifact/runtime identity, and unchanged prompt/tools. `config_only`, `llm_only`, and `combined` reject changes outside their declared axes. Combined tests cannot isolate causal effects of either axis. Old model backends are not uninstalled or modified, including those referenced by Context/Coordinator.

Exactly 20 registered synthetic SendMessage calls are permitted per experiment: 8 normal, 4 limit, 4 metadata, 2 missing-user and 2 empty-result cases. Each creates one conversation; Istio probabilistically assigns its release. No pairing, replay, balancing top-up, extra inference readiness probes or significance claims. Deployment/fault-injection tests below are separate from that budget.

## One-time bootstrap (required before production traffic)

1. Build/push the router image for the cluster architecture, then use the **registry digest**, not a mutable tag. Example build: `docker buildx build --platform linux/amd64 -f apps/agentic/llm_ab_router/Dockerfile -t REGISTRY/recsys-llm-ab-router:GIT_SHA --push .`. The local-test image is not a production image and was not pushed.
2. Provision a dedicated PostgreSQL database/user for `recsys_ab` and a versioning-enabled MinIO bucket. Do not point the schema migration at kagent's internal tables. Create Kubernetes Secret `recsys-llm-ab-runtime` in `kagent` with `AB_DATABASE_URL`, `AB_INTERNAL_TOKEN`, `AB_STATE_URI`, `MODEL_STORE_ENDPOINT`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`. Generate a strong independent internal token; do not commit credentials. The router role needs DDL for its own schema, not access to kagent tables. Prefer separate runtime read-only S3 credentials and CD read/write credentials for the state prefix.
3. Create Jenkins **Secret file** credential `recsys-llm-ab-env`, containing a JSON object with the same database/token/S3 connection settings (excluding `AB_STATE_URI`, supplied by the job). Optional keys: `AB_ROUTER_URL`, `AB_NAMESPACE`, `AB_SECRET_NAME`. Never use a shell script as this credential. CD uses a separate virtualenv because the repository's pinned boto3 1.35.36 does not provide the required conditional PutObject interface.
4. Export a manifest with the exact public GGUF URL and verified SHA256, using `python -m jenkins.python.llm_agent_cd.snapshot --artifact-url URL --artifact-sha256 SHA256 --output .llm-agent-cd/champion.json`. This reads the existing Recommendation Agent, ModelConfig and llama deployment without reading Secret values. It includes the **effective** prompt, including its existing revision text. The new managed backend downloads/checksums model bytes before startup; no alias-only attestation is accepted. Shared external backends instead require an independently verified attestation ConfigMap with `llm_version_id` and `artifact_sha256`, and `binding.health_url` if the inference gateway does not expose `/health`.
5. Set `AB_STATE_URI`, `AB_ROUTER_IMAGE` and the connection environment, then run `python -m jenkins.python.llm_agent_cd bootstrap --champion .llm-agent-cd/champion.json`. This is **create-only** and cannot overwrite an existing state object.
6. Install `infra/helm/recsys-llm-ab` as Helm release `recsys-llm-ab` in `kagent`, with `--set-string image=REGISTRY/IMAGE@sha256:DIGEST`. It creates a private gateway, router Deployment and BYO A2A facade. Namespace-wide injection is not enabled. The namespace must not carry `istio-injection=disabled`; the gateway opts in at pod level. Confirm that its injected `istio-proxy` image is not the literal placeholder `auto`.
7. Run `python -m jenkins.python.llm_agent_cd activate`. It creates/verifies the immutable bootstrap release and 100% baseline route without generating tokens. It may be rerun while `IDLE` until both gateways acknowledge the route. Success creates `recsys-ab-activation`; the normal Coordinator deploy then permanently includes `configs/llm-ab/coordinator-router.yaml`. Run that normal dependency-closed deploy, including golden-image rebuild. The original agent remains for existing conversations; **new UI conversations must select `recsys-recommendation-router`**. The old direct endpoint is not transparently intercepted. A/B preflight refuses to start until Coordinator references the router.
8. Deploy the updated CI/observability charts. Jenkins seeds `RecSys-LLM-Agent-CD`; Grafana provisions `LLM Agent A/B Rollout` (`recsys-llm-ab`). The Prometheus scrape of the router Service reads shared DB-backed aggregates once, avoiding replica double counting.

The current implementation is single-namespace (`kagent`) with the default gateway/router names. Keep Helm values, `AB_NAMESPACE`, the manifest bindings and the Coordinator overlay aligned. Do not run concurrent CLI mutators outside the Jenkins `recsys-production-release` lock.

## Candidate preparation and running

Copy the exported manifest, remove its three computed `*_id` fields, and change only the declared axis. `config_id` excludes endpoint/model alias; `llm_version_id` includes public artifact URI/checksum, quantization, serving image digest and every supported serving flag. For a new managed LLM, compute the new identity with `release(manifest)` and set `binding.backend_url` to `http://rec-llm-<first 20 llm_version_id chars>.kagent.svc.cluster.local:8000/v1`; add that host to allowed domains. `config_only` must reuse the exact binding. No agent prompt revision is injected from an experiment hash.

The managed backend requests 2 CPU / 3 GiB and limits 2 CPU / 5 GiB; scheduling, artifact verification or startup failure stops delivery before weight changes. Existing healthy external backends are never modified. This is not a capacity reservation and does not guarantee the small GKE cluster has room for both model versions.

Verify fixture preconditions against the inference service before running: users `1001..1008`, candidates `101..103`, and candidate `-1` for the two empty cases are **example data bindings**, not claimed live seeded results. Replace these fixture inputs with known valid/empty-result records in your environment while retaining the 8/4/4/2/2 categories and exactly 20 unique cases. An invalid empty fixture fails; the implementation never manufactures an empty tool response.

Register an immutable model alias, publish its emitted `langfuse_config`, and
move `ab-ready` to that prompt version. The poller claims it and dispatches the
`prepare` action. Start the one Traffic Job with:

```bash
uv run python -m jenkins.python.llm_agent_cd.llm_ab_traffic start \
  --experiment-id rec-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  --entrypoint public-a2a --follow
```

The controller then dispatches short Jenkins builds for 10%, 50%, 100%,
promotion and cleanup. Operators do not invoke Jenkins or edit Istio. Request
intents are committed before HTTPS; an unresolved result remains HOLD and is
never resubmitted.

Each promotion stage needs >=10 minutes and >=5 completed production invocations per relevant arm. At 50%, exactly 20 synthetic outcomes must also pass with >=5 on each arm. Candidate p95 must be <=1.2x the concurrent champion p95. Missing/stale/NaN evidence is HOLD; after 60 minutes the stage rolls back instead of topping up. Twenty cases are functional evidence, not a powered superiority experiment.

**Evidence contract:** the pinned A2A history must contain the invocation's tool call and response. The evaluator compares exact arguments, item ordering, scores and metadata to machine-readable final JSON. The 20 prompts explicitly request that JSON. Production prose that cannot be verified is HOLD, not automatically a tool-contract failure or a pass. Therefore promotion requires enough production conversations with machine-verifiable output. Optional `task.metadata.usage.{input_tokens,output_tokens}` is recorded per invocation if supplied; absent usage remains unavailable and is never inferred from a shared backend's aggregate counters.

## Failure, privacy and retention

The stable router persists `(principal, context) -> release` in PostgreSQL. Session advisory locks serialize concurrent callers, and a committed invocation claim prevents a duplicate message ID executing twice. Different backend contexts are derived for each release. Polling is principal-bound; completed tasks cannot be cancelled. Streaming is not advertised. No request retry, mirroring or cross-release replay is configured.

Rollback quarantines the failing release immediately, routes new sessions to the immutable baseline, waits up to 120 seconds for all ready gateway proxies plus a real pinned `/identity` probe, verifies the old agent/backend, then commits the champion pointer. Failed-release conversations receive a restart-required error rather than silently changing model mid-conversation. Existing in-flight tool calls cannot be undone. A rollback that cannot be verified becomes `ROLLBACK_FAILED`; repair infrastructure then invoke `rollback` again.

After promotion the experiment immediately becomes `COMPLETED`; there is no
monitor gate. A missing Traffic Job, missing scores, stale telemetry or zero
samples remains HOLD during the active stage. Hard failures trigger a separate
rollback action, and the 60-minute stage timeout rolls back rather than creating
replacement traffic.

Task responses are stored in the dedicated DB to support idempotent A2A retrieval; treat this as sensitive application data, use encrypted storage/backups and restricted DB credentials. MinIO status artifacts contain redacted assertion results, trace IDs and synthetic fixtures, not raw production prompts/results. Traces forward only release/source IDs and status, retaining the existing Collector content-redaction pipeline. No request/session IDs become Prometheus labels.

After a verified terminal route, the controller dispatches cleanup. It expires
test sessions, prunes unreachable pin routes, verifies Envoy, and scales only
unreferenced controller-owned adapters/backends to zero. Champion, previous,
production sessions and shared consumers remain protected; manifests and
evidence are retained.

## Verification and evidence capture

- Local: `python -m pytest -q tests/unit/jenkins/test_llm_agent_cd.py`.
- PostgreSQL integration: run a disposable localhost database and set `LLM_AB_TEST_DATABASE_URL`, then run `python -m pytest -q tests/integration/test_llm_ab_router.py`. Tests refuse non-local DSNs and truncate **only the disposable `recsys_ab` schema**.
- Chart checks: Helm lint/render and Kubernetes server-side dry-run. Gateway propagation is read from each actual proxy via `pilot-agent request GET config_dump`, followed by a data-plane identity probe; reading a VirtualService object alone is not proof.
- For real acceptance, capture Jenkins stages, the 10/50/100 weights and actual assignment split, 20-case result table, traces for both config/LLM pairs, champion state and a separate controlled rollback experiment. Archive `.llm-agent-cd/status.json` and its event timeline. Never substitute unit-test mocks or dashboard mockups for these screenshots.

At implementation time, local tests, image build and server-side schema validation are distinct from a live experiment. No production traffic shift, production 20-case run, 24-hour monitoring completion or screenshot proof should be claimed until bootstrap prerequisites above are supplied and the real job completes.

References: [Istio traffic shifting](https://istio.io/latest/docs/tasks/traffic-management/traffic-shifting/), [Istio injection rules](https://istio.io/latest/docs/setup/additional-setup/sidecar-injection/), [proxy config verification](https://istio.io/latest/docs/ops/common-problems/security-issues/), [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html), [pinned kagent A2A transport](https://github.com/kagent-dev/kagent/blob/e6df917e9fa8/go/core/internal/a2a/substrate_sandbox_transport.go).
