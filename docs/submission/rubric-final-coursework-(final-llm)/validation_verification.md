# Substrate 0.0.11 Canary, Production Gate, and Rollback Evidence

Date: 2026-08-26. Production project: `recsys-mlops-506406`.

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
- CPU PromQL thresholds (`120` microcores for context and `400` for
  recommendation).

The Substrate control-plane/ATE images returned to `0.0.6`, while the Valkey
storage image remains pinned to `9.1` because the upgraded AOF base files are
not readable by Valkey `8.0`. This is an intentional storage-compatibility pin,
not a partial Substrate runtime upgrade.

Valkey returned to `cluster_state:ok` with all `16384` slots. Six ephemeral
`worker:*` keys written by the incompatible runtime were removed after being
enumerated, and clean `0.0.6` worker registrations were recreated. No disk
snapshot restore was required.

Post-rollback smokes proved healthy Substrate transport and application paths:

- recommendation specialist A2A reached GetActor/ResumeActor successfully;
- coordinator regular A2A returned `COORDINATOR-OK`;
- eight concurrent coordinator requests at concurrency four completed while
  the Deployment remained `1/1`;
- no coordinator SandboxAgent, WorkerPool, ScaledObject, HPA, or PDB remains.

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
topic has the same mismatch and still requires a separately approved repair;
until then Flink reports `NOT_COORDINATOR`, candidate keys cannot be
repopulated, and the recommendation smoke remains HTTP 502. Kafka and Kafka
Connect are Ready, and the GCP event producer is returned to its configured
zero replicas.

The restored CPU autoscaler itself is healthy. A final context-only A2A load
made KEDA/HPA move the Context WorkerPool from one to three replicas with
`ValidMetricFound=True`, scaler health `Happy`, and no fallback. This proves the
supported CPU path; it is not evidence for the rejected `0.0.11`
assigned-worker design.

## Current supported state

Until a newer kagent/Substrate pair passes the same canary and production A2A
gate, the supported production state is:

```text
GKE                         1.35.7-gke.1027000
certificate beta APIs      enabled (one-way)
kagent                      0.9.9
Substrate                   0.0.6
specialist autoscaling      CPU-based KEDA, 1..3, fallback 1
coordinator                 regular Agent, fixed replica 1
recommendation data smoke   blocked by Kafka/Zookeeper topic-ID recovery
```

Historical `0.0.6` specialist scale screenshots remain valid for the restored
CPU configuration. Any draft assigned-worker `1 -> 2 -> 3 -> 1` claim is
superseded by this failed compatibility gate because that load test was never
authorized to run after the gate turned red.

## Reproduction and rollback entrypoints

- [enable the one-way GKE APIs](../../../ops/gcp/enable_substrate_cert_beta_apis.sh)
- [certificate compatibility preflight](../../../ops/validation/substrate_gke_compatibility.sh)
- [mTLS preparation helper](../../../ops/gcp/prepare_substrate_mtls.sh)
- [coordinator fixed-replica concurrency](../../../ops/validation/coordinator_agentic_concurrency.sh)
- [Terraform GCP wrapper](../../../ops/gcp/terraform_gcp.sh)

Do not attempt to disable the beta APIs or downgrade production GKE as part of
a future application rollback. Restore disk snapshots only if the rolled-back
runtime cannot read the existing storage.
