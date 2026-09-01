# kagent referenced-Secret rotation proof -- 2026-08-29

This controlled production-cluster canary tested the kagent operational claim
that an Agent automatically restarts when a referenced Secret changes. It used
a disposable Secret, ModelConfig, SandboxAgent, and dedicated WorkerPool in the
`kagent` namespace. The real `kagent-agent-gateway` credential was neither read
nor changed.

## Result

**Met for the Agent Substrate `SandboxAgent` path after compatibility build
`0.10.0-e6df917-substrate0011-v8`.** A referenced-Secret update changed the
ModelConfig hash, created a new rollout-relevant ActorTemplate, and caused
Agent Substrate to create, resume, and suspend a new golden runtime.

## Remediation

The pinned upstream source already watched referenced Secrets, but its
Substrate backend stored the propagated config hash only as ActorTemplate
metadata. Metadata does not contribute to `ActorTemplateSpec`, so the template
shape and golden actor remained unchanged.

The repository compatibility patch now projects the short config hash into the
literal `KAGENT_CONFIG_REVISION` environment variable in
`ActorTemplateSpec`. The existing shape hash therefore creates a new immutable
template and golden for every config revision. Existing session actor IDs and
durable data remain stable; their configured cold-resume path re-resolves the
live Secret.

Build and deployment provenance:

```yaml
sourceCommit: e6df917e9fa8
compatibilityBuild: 0.10.0-e6df917-substrate0011-v8
cloudBuildID: 67772eb7-4218-48c0-8a68-af6a5753b7c6
cloudBuildStatus: SUCCESS
controllerDigest: sha256:b319fb2c2479be0132b1ba047ecd3115a9e4fc665eb8774407ab0a13bb884131
helmRevision: 27
controllerReplicas: 3/3
```

The exact reproducible implementation is retained in the
[`kagent-e6df917-substrate0011.patch` bridge (line 831)](../../../../../ops/gcp/patches/kagent-e6df917-substrate0011.patch#L831)
source patch and is protected by `TestBuildActorTemplateShapeHashIdentity`.

## Method

1. Build the patched source at commit `e6df917e9fa8`; run the ADK and Substrate
   package tests; push all `v8` images.
2. Rolling-upgrade the three-replica controller with Helm
   `--rollback-on-failure`, then confirm 3/3 Ready and a renewing Lease.
3. Apply the retained [`canary-resources.yaml`](canary-resources.yaml) and wait
   for ModelConfig Accepted, WorkerPool 2/2, and SandboxAgent Ready.
4. Verify the v1 hashes, ActorTemplate identity, golden actor ID, resource
   versions, and literal rollout env.
5. Change only the disposable `PROOF_TOKEN` marker from `rotation-v1` to
   `rotation-v2`.
6. Verify both ATE API replicas, the new ActorTemplate, new golden ID, and
   runtime lifecycle RPCs.
7. Restore v1, confirm the original ModelConfig hash, and remove every canary
   resource.

No Secret value was printed or retained in command output. The literal values
in the manifest are disposable proof markers, not credentials.

## Before fix: failing control

The original `v7` run proved that Secret watch/hash propagation worked but the
runtime did not roll:

| Signal | Baseline v1 | Rotated v2 |
|---|---|---|
| ModelConfig `status.secretHash` | `7e89...a172` | `e9fa...4bd5` |
| ActorTemplate `kagent.dev/config-hash` | `4443...8ce8` | `f1d9...b8cb` |
| ActorTemplate name | `secret-rotation-proof-4e8d9b0e5095db29` | unchanged |
| Golden actor ID | `3adc...ebaf` | unchanged |
| Lifecycle RPCs in 51 seconds | — | `0` |

This is the failing control that established the regression and is preserved
under `beforeFix` in [`observations.yaml`](observations.yaml).

## After fix: passing E2E proof

| Signal | Baseline v1 | Rotated v2 | Proof |
|---|---|---|---|
| Source Secret resource version | `1787945417784351020` | `1787945466438639020` | Kubernetes accepted the update |
| ModelConfig `status.secretHash` | `7e89...a172` | `e9fa...4bd5` | Secret watch reconciled |
| ActorTemplate config revision | `4443...8ce8` | `f1d9...b8cb` | Hash reached spec and metadata |
| ActorTemplate name | `...-ee291dbe231a88d1` | `...-a2d56e690c07948c` | Shape changed |
| Golden actor ID | `706658d3...` | `07e60e80...` | New runtime identity |
| ActorTemplate phase | `Ready` | `Ready` | New golden became serviceable |
| ATE lifecycle RPCs | baseline golden already Ready | Create + Resume + Suspend | Runtime transition occurred |

Exact post-rotation runtime evidence:

```yaml
createActor:
  time: 2026-08-28T19:31:06.533380403Z
  actorTemplate: secret-rotation-proof-a2d56e690c07948c
  goldenActorID: 07e60e80-7a2e-4c7b-b891-11d3d55d0e07
resumeActor:
  time: 2026-08-28T19:31:06.943740284Z
  resumed: true
suspendActor:
  time: 2026-08-28T19:31:07.294809713Z
  snapshotID: 4bfe85d9-9b5d-42cc-8b3e-6047f21f2834
```

Restoring v1 returned the ModelConfig hash to
`7e89e1a2d8dff4088f11855c53788b693540a6946f61373adfe019553dfea172`.
The final check at `2026-08-28T19:38:17Z` found no canary resource or Pod.
The first manifest-wide delete exposed a cleanup-order edge case because it
removed the ModelConfig before the SandboxAgent finalizer ran. The disposable
dependencies were recreated, the terminating SandboxAgent was reconciled to
completion, and dependencies were then deleted in SandboxAgent-first order.

## Production safety after cleanup

The production controller remained 3/3 Ready on `v8`; the Kubernetes Lease was
renewing with a 15-second duration. All three production SandboxAgents were
Ready and Accepted, and all three production WorkerPools were 1/1. The default
ModelConfig hash remained
`f4885724ee2f90009bef3af56de6e0cba87414f44c8df1706e98b1fcef9f9276`.
The real Secret still exposed only `AGENT_GATEWAY_API_KEY` and remained owned by
`ExternalSecret/kagent-agent-gateway`.

Relevant references:

- [kagent automatic restart documentation](https://kagent.dev/docs/kagent/operations/operational-considerations/#automatic-agent-restart-on-secret-updates)
- [Pinned ModelConfig Secret watch](https://github.com/kagent-dev/kagent/blob/e6df917e9fa8/go/core/internal/controller/modelconfig_controller.go)
- [Pinned ModelConfig Secret hash reconciliation](https://github.com/kagent-dev/kagent/blob/e6df917e9fa8/go/core/internal/controller/reconciler/reconciler.go)
- [Pinned Substrate lifecycle implementation](https://github.com/kagent-dev/kagent/blob/e6df917e9fa8/go/core/pkg/sandboxbackend/substrate/agent_lifecycle.go)
