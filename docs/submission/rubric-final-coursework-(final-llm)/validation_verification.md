# Substrate 0.0.11 rollout and assigned-worker validation

## Current production result (27 August 2026)

The current source target supersedes the regular-Coordinator/CPU topology
described in the historical incident record below:

```text
kagent                      0.10.0-e6df917-substrate0011-v6
Substrate                   0.0.11 mTLS
Context                     SandboxAgent + assigned-worker KEDA 1..3
Recommendation              SandboxAgent + assigned-worker KEDA 1..3
Coordinator                 SandboxAgent + assigned-worker KEDA 1..3
assigned threshold          AverageValue 0.7
scale-down/fallback         300 seconds / 1 replica after 3 failures
```

The compatibility and production gates are green. Substrate control plane,
RustFS, Valkey `9.1`, all three SandboxAgents, WorkerPools, ScaledObjects, HPAs,
and generated worker Deployments returned Ready after the tests. The native
ATE endpoint and Prometheus both exposed
`ate_workerpool_workers{ate_worker_state="assigned"}` with namespace, pool, and
state labels. WorkerPool `/scale` reported spec/status replicas and a native
`.status.selector`.

| Production proof | Result |
| --- | --- |
| Context assigned-worker scale | `1 -> 2 -> 3 -> 2 -> 1`; 6,195 offers, 7 grounded completions, 6,188 expected backpressure responses |
| Recommendation assigned-worker scale | `1 -> 2 -> 3 -> 2 -> 1`; 2,187/2,187 requests completed |
| Coordinator assigned-worker scale | `1 -> 2 -> 3 -> 2 -> 1`; load generator recorded 240 offers |
| Missing-Prometheus behavior | all three ScaledObjects reached `Fallback=True` and held one replica; endpoints were restored |
| Coordinator v19 routing | context-only, recommendation-only, composite, both direct MCP cases, and partial failure passed |
| Composite exact-call contract | `Recommendation Agent -> Context Agent`, exactly once each |

The v6 kagent patch uses a stateful per-invocation duplicate-call guard and a
Coordinator-only explicit-selection state machine. This resolves the earlier
wire-format/retry interaction without changing generic tool routing. Clean
source tests passed for `./adk/pkg/agent` and
`./core/pkg/sandboxbackend/substrate`; repository contract tests also passed.

The production model revisions are Context v8, Recommendation v7, and
Coordinator v19. Routing evidence is stored in
`reports/agentic/recsys-coordinator-agent-sandbox-a2a-v19.json`; autoscale
helpers captured metric, ScaledObject, HPA, WorkerPool, generated Deployment,
pod, scale-down, and fallback state. Historical evidence follows and is
retained for rollback/audit purposes, but it no longer describes production.

## Historical, superseded Substrate 0.0.11 gate and rollback evidence

Date: 2026-08-26, revalidated 2026-08-27. Production project:
`recsys-mlops-506406`.

## Outcome

The rollout stopped at the compatibility gate and rolled back safely.

- Canary GKE `1.35.6-gke.1710000` proved both certificate beta APIs,
  Substrate `0.0.11` mTLS, projected credential/trust files, and native
  `ate_workerpool_workers` labels.
- Production was upgraded to GKE `1.35.7-gke.1027000`; both beta APIs remain
  enabled because GKE does not support disabling them.
- Substrate `0.0.11` storage, control plane, and metrics became healthy after
  the known Valkey announce-port compatibility patch.
- kagent `0.9.9` A2A calls then failed with an invalid protobuf wire format.
- The 15-minute compatibility gate was therefore red. Substrate/ATE returned
  to `0.0.6`, specialist KEDA returned to CPU queries, and assigned-worker
  scaling was not deployed or claimed as production evidence.
- The coordinator migration was independent and remains live as regular
  `Agent/recsys-coordinator-agent`, fixed at one replica.

The 2026-08-27 retry observed the assigned-worker series for all three planned
pool names, but stopped before deploying assigned-worker ScaledObjects because
the same A2A wire-format gate failed. The rollback kept the two specialists as
SandboxAgents on CPU scaling. The attempted Coordinator SandboxAgent could not
be recovered safely: its 0.0.11 golden-snapshot prefix was empty and kagent's
cleanup/reconcile path hit incompatible persisted actor records. Per the
coordinator-specific fallback, production returned to the regular one-replica
Coordinator and removed its sandbox pool/scaler resources.

## Canary evidence

The temporary resources used the planned isolated address space:

```text
cluster: recsys-substrate-0011-canary
subnet:  recsys-substrate-canary
primary: 10.60.32.0/24
pods:    10.60.0.0/20
service: 10.60.16.0/24
```

JWT mode failed because the worker certificate signer was unavailable. A clean
mTLS retry succeeded. The worker contained both projected files:

```text
/run/podidentity.podcert.ate.dev/credential-bundle.pem
/run/clustertrustbundle.ate.dev/trust-bundle.pem
```

The direct metrics endpoint exposed `ate_workerpool_workers` with the required
labels `ate_workerpool_namespace`, `ate_workerpool_name`, and
`ate_worker_state`. After the production gate failed, both the canary cluster
and subnet were deleted.

## Production safety evidence

Before mutation, the rollout saved Terraform/Helm state and created seven
READY Persistent Disk snapshots:

```text
gs://recsys-mlops-506406-tfstate/terraform/gcp/backups/
  pre-substrate-0011-20260826T161500.tfstate

recsys-sub0011-20260826-rustfs
recsys-sub0011-20260826-valkey0 ... recsys-sub0011-20260826-valkey5
```

The control plane, two CPU nodes, and ML-system node reached
`1.35.7-gke.1027000`. API discovery showed:

```text
certificates.k8s.io/v1beta1/podcertificaterequests
certificates.k8s.io/v1beta1/clustertrustbundles
```

Substrate `0.0.11` mTLS reached a healthy control plane and emitted the native
metric through both its direct endpoint and Prometheus. The decisive A2A gate
failed with:

```text
grpc: error unmarshalling request: proto: cannot parse invalid wire-format data
```

This demonstrates that certificate and metric support alone is insufficient;
kagent `0.9.9` and Substrate `0.0.11` do not pass this repository's production
A2A contract.

## Rollback evidence

The rollback restored:

- Substrate chart/runtime `0.0.6`;
- Substrate CRDs `0.0.6` and WorkerPool `scaleSelector` compatibility
  post-renderers;
- context and recommendation `ateom-gvisor:v0.0.6` images;
- original model configuration revisions;
- CPU PromQL thresholds (`200` microcores for context and `400` for
  recommendation).

The Substrate control-plane/ATE images returned to `0.0.6`, while the Valkey
storage image remains pinned to `9.1` because the upgraded AOF base files are
not readable by Valkey `8.0`. This is an intentional storage-compatibility pin,
not a partial Substrate runtime upgrade.

Valkey returned to `cluster_state:ok` with all `16384` slots. After the GKE
node recreation, each persisted `nodes.conf` still advertised its former Pod
IP even though the cluster reported healthy slots. The Substrate post-renderer
now injects the Downward API `POD_IP` and starts Valkey with
`--cluster-announce-ip`, preventing `GetActor` from following stale topology.
Six ephemeral
`worker:*` keys written by the incompatible runtime were removed after being
enumerated, and clean `0.0.6` worker registrations were recreated. No disk
snapshot restore was required.

Post-rollback smokes proved healthy Substrate transport and application paths:

- recommendation specialist A2A reached GetActor/ResumeActor successfully;
- coordinator regular A2A returned `COORDINATOR-OK`;
- eight concurrent coordinator requests at concurrency four completed while
  the Deployment remained `1/1`;
- no coordinator SandboxAgent, WorkerPool, ScaledObject, HPA, or PDB remains.

On the 2026-08-27 evidence run, Context and Recommendation A2A smokes passed
again. The regular Coordinator passed readiness, direct MCP, and A2A transport,
but its full five-case routing assertion did not pass after three attempts:
one attempt bypassed the specialist for direct RAG MCP, one duplicated the RAG
call, and one recommendation-specialist request timed out. This is an
application routing gate failure, not a Substrate rollback-health failure.
Jenkins and registry publication were intentionally not run.

Terraform state cleanup removed only the four temporary `0.0.11` entries
(`substrate-mtls-bootstrap`, the Coordinator sandbox WorkerPool, certificate
controller namespace ownership, and the immutable-migration marker) without
deleting live CA secrets. A new production plan reported `0 to destroy`; its
remaining in-place drift includes an unrelated Cloud Logging import plus Helm
provider reconciliation, so the mixed plan was deliberately not applied whole.

The final recommendation MCP smoke then exposed a separate data-plane incident
caused by the GKE node rotation. Zookeeper mounted its existing PVC but loaded
an empty `zxid 0x0` snapshot. Kafka retained its PVC and rejected the new
Zookeeper cluster ID. Restoring `/cluster/id` to the value in Kafka's persisted
`meta.properties` brought the broker back without deleting messages. The source
Postgres PVC was also expanded online from `20Gi` to `30Gi` after the event
generator reported `No space left on device`; no table was truncated.

The 13 RecSys CDC/Kafka Connect topic IDs were then restored from their
persisted `partition.metadata` values. Kafka `ListOffsets` recovered and Kafka
Connect loaded `recsys-postgres-cdc` from its compacted config topic without
deleting a log directory, topic, or PVC. The internal `__consumer_offsets`
topic ID was also restored from its persisted 50-partition metadata. The
realtime producer was enabled temporarily, repopulated `92` candidate keys and
`160` global candidates, and returned to its configured zero replicas. Direct
Recommendation MCP smoke now passes.

The repository target uses deterministic Qwen settings (`temperature=0`,
`seed=42`, `maxTokens=384`), while the restored kagent Helm revision still
reported its pre-rollout `maxTokens=768` value. The regular coordinator remains
fixed at one replica. Isolated
context-only and recommendation-only A2A routing gates pass, as do direct MCP
protocol checks. The composite gate remains red on kagent `0.9.9`: after the
Recommendation SandboxAgent returns, Qwen may call the coordinator's direct
RAG MCP tool instead of the required Context SandboxAgent. The partial-failure
gate is also red: after a direct RAG lookup returns the expected non-retryable
HTTP 404 while the recommendation call succeeds, Qwen repeats both tools rather
than returning the usable recommendation with a partial-result warning. Because
the full routing contract is not green, the new regular-agent registry artifact
was not published and the legacy sandbox registry artifact was deliberately
retained.

The restored CPU autoscaler itself is healthy. A final context-only A2A load
made KEDA/HPA move the Context WorkerPool from one to three replicas with
`ValidMetricFound=True`, scaler health `Happy`, and no fallback. This proves the
supported CPU path; it is not evidence for the rejected `0.0.11`
assigned-worker design.

The 2026-08-27 rollback calibration found that summing worker CPU creates a
replica-count feedback loop because every idle gVisor worker contributes roughly
93–185 microcores. The supported CPU query now uses the hottest worker
(`max(rate(...))`) with `AverageValue`; production targets are 200 microcores
for Context and 400 for Recommendation. Context then scaled `3 -> 2 -> 1` after
the configured 300-second stabilization. Fresh concurrent A2A loads drove both
specialists from `1 -> 3`; Context recorded one grounded completion and 19
backpressure rejections, while Recommendation completed 20/20 requests. Both
capture helpers now allow a 60-second post-load KEDA decision grace so a short
successful load is not reported as a false negative. At the one-replica
baseline, both specialists entered `Fallback=True` after three Prometheus
failures while HPA desired remained `1`; each test restored the original
endpoint automatically. The verification timeout is now 420 seconds so the
same proof can coexist with an active 300-second scale-down history.

## Historical supported state after the failed 0.0.11 gate (superseded)

The following table records the temporary rollback state before the v6 build
passed the same production A2A gate. It is not the current supported state:

```text
GKE                         1.35.7-gke.1027000
certificate beta APIs      enabled (one-way)
kagent                      0.9.9
Substrate                   0.0.6
specialist autoscaling      CPU-based KEDA, 1..3, fallback 1
coordinator                 regular Agent, fixed replica 1
recommendation data smoke   pass
coordinator simple routing  context-only and recommendation-only pass
coordinator composite       blocked: 0.8B model bypasses Context Agent
coordinator partial failure blocked: 0.8B model repeats failed tool pair
registry cutover            withheld; legacy artifact retained
```

Historical `0.0.6` specialist scale screenshots remain valid for the restored
CPU configuration. Any draft assigned-worker `1 -> 2 -> 3 -> 1` claim is
superseded by this failed compatibility gate because that load test was never
authorized to run after the gate turned red.

## Worker warm-up benchmark

The repository keeps a version-aware benchmark for an already-ready specialist
WorkerPool:

```bash
make agent-substrate-warmup-benchmark
```

It defaults to the Recommendation SandboxAgent, verifies the SandboxAgent and
WorkerPool are Ready, runs three A2A iterations, records per-request latency,
and captures the ActorTemplate, WorkerPool, worker Pods, ScaledObject, ATE API
logs, and raw responses under `reports/agentic/substrate-warmup-<UTC run ID>/`.
Context can be selected explicitly:

```bash
SUBSTRATE_BENCHMARK_AGENT=recsys-context-agent-sandbox \
SUBSTRATE_BENCHMARK_WORKER_POOL=recsys-context-sandbox-pool \
make agent-substrate-warmup-benchmark
```

Warm-up output must always record the active runtime version. The current
production baseline is Substrate `0.0.11` with kagent v6. Warm-up latency alone
is still not assigned-worker autoscaling evidence; the scaler proof must also
capture the metric, HPA, WorkerPool, generated Deployment, and fallback state.

## Reproduction and rollback entrypoints

- [enable the one-way GKE APIs](../../../ops/gcp/enable_substrate_cert_beta_apis.sh)
- [certificate compatibility preflight](../../../ops/validation/substrate_gke_compatibility.sh)
- [mTLS preparation helper](../../../ops/gcp/prepare_substrate_mtls.sh)
- [coordinator assigned-worker autoscale proof](../../../ops/validation/coordinator_agentic_autoscale.sh)
- [coordinator routing smoke](../../../ops/validation/coordinator_agentic_smoke.sh)
- [Terraform GCP wrapper](../../../ops/gcp/terraform_gcp.sh)

Do not attempt to disable the beta APIs or downgrade production GKE as part of
a future application rollback. Restore disk snapshots only if the rolled-back
runtime cannot read the existing storage.
