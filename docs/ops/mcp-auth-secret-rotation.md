# Zero-downtime MCP authentication Secret rotation

This runbook rotates Recommendation and Feature/RAG MCP credentials without
changing a Secret in place. It uses immutable, Vault-version-pinned Secrets,
parallel MCP workloads, and a stable kagent `RemoteMCPServer`/`SandboxAgent`
identity. It does not use Reloader, a kagent patch, or dual-token application
logic.

The non-secret source of truth is
`configs/agentic/mcp-auth-versions.yaml`. Change the lifecycle of exactly one
service per commit. Rotate Recommendation first; complete its retirement and
purge before starting Feature/RAG.

## Invariants

- `activeRevision` exists and remains `deploy: true`.
- A `vN` revision uses Vault KV version `N`, Secret `<base>-vN`, and workload
  `<base>-vN`.
- Prepare only adds one `deploy: true` revision.
- Cutover or rollback changes only `activeRevision`.
- Retirement only changes one inactive revision from `deploy: true` to
  `deploy: false`.
- Purge only removes a revision already applied as `deploy: false`.
- Terraform runs before Jenkins for prepare. Jenkins runs before Terraform for
  retirement. Purge is a later Terraform apply with separate approval and the
  retirement evidence file.
- The managed rotation path requires Vault, ESO, and Istio telemetry whenever
  `deploy_llm_inference=true`; Terraform rejects the non-versioned Kubernetes
  SecretStore fallback for this production workflow.
- Production MCP images remain digest-pinned. A rotation-only release reuses
  the currently installed immutable image reference for every slot.
- Keep enough free WorkerPool capacity to build the new kagent golden actor;
  a single busy worker cannot provide a zero-downtime cutover.
- With Substrate `v0.0.9`, KEDA targets the WorkerPool-owned
  `<worker-pool>-deployment`. The `WorkerPool` `/scale` status has no selector
  in that release, so targeting the CR directly leaves the HPA in
  `InvalidSelector`. Preflight verifies the generated Deployment owner,
  selector, ScaledObject target, and `ScalingActive=True` HPA condition.

Validate the file and a proposed transition locally:

```bash
python3 ops/security/mcp_auth_versions.py validate
python3 ops/security/mcp_auth_versions.py validate-transition \
  /path/to/previous-mcp-auth-versions.yaml \
  configs/agentic/mcp-auth-versions.yaml
```

## One-time migration from mutable legacy Secrets

The checked-in bootstrap manifest retains the existing `legacy` resource and
adds immutable `v1` resources pinned to Vault version 1, while leaving
`activeRevision: legacy`.

1. Apply the reviewed Terraform stack. Terraform validates the same manifest,
   installs/updates `recsys-security`, and waits for every retained
   ExternalSecret and target Secret. Do not start the Jenkins consumer rollout
   first; its preflight intentionally fails closed while `v1` is absent.
2. Run the normal Jenkins component release for the same commit. It creates
   both MCP `v1` slots with the currently deployed image digest, runs the
   same-token auth matrix, and performs a functional candidate MCP smoke.
3. In a Recommendation-only commit, change its `activeRevision` from `legacy`
   to `v1`. Run Jenkins. It probes both slots continuously across the agent
   upgrade, waits for the exact ActorTemplate desired generation, executes a
   fresh-session Recommendation A2A smoke, and then runs the Coordinator
   composite smoke.
4. Repeat the cutover commit for Feature/RAG and run the Context plus
   Coordinator verification.
5. Retire and purge each `legacy` revision with the separate procedure below.
   The Vault write helper stays locked until the active revision is versioned
   and `legacy` is no longer deployed.

## Rotate a versioned credential

Confirm the active revision and current Vault version, then supply the new
token on stdin. The helper refuses shell xtrace, validates CAS, writes both
`MCP_AUTH_TOKEN` and `Authorization`, keeps its temporary files mode `0600`,
and prints only the new numeric Vault version. Before it reads stdin, it also
requires the active versioned Deployment and immutable ExternalSecret to be
Ready at that exact Vault version. If `legacy` is retained, the reviewed
retirement evidence, live `deploy=false` ExternalSecret annotation, and absence
of legacy Deployment/Pods must all agree; if it was purged, no legacy
ExternalSecret may remain live. After a rollback, pass the actual current Vault
head to CAS even when it is newer than `activeRevision`; the active slot is
validated against its own pinned version and remains untouched.

```bash
read -r -s MCP_ROTATION_TOKEN
printf '%s\n' "${MCP_ROTATION_TOKEN}" \
  | ops/security/rotate_mcp_auth_vault.sh recommendation 1
unset MCP_ROTATION_TOKEN
```

If the helper prints `2`, create a prepare commit adding `v2` with
`vaultVersion: "2"`, the `-v2` Secret/workload names, and `deploy: true`.
Leave `activeRevision` unchanged.

1. Apply Terraform first. Confirm the new ExternalSecret and immutable Secret
   are Ready.
2. Run the Jenkins release. The MCP deployment preflight waits for Terraform,
   builds the green slot, runs the distinct-token matrix (own token succeeds,
   cross/missing/invalid token returns 401), and performs the candidate tool
   smoke. It also expands the specialist allowlist without changing the active
   MCP endpoint.
3. Create a separate cutover commit changing only `activeRevision` to `v2`.
4. Run Jenkins. The stable RemoteMCPServer changes its URL and Secret reference
   together; the literal revision env creates the new actor shape. Continuous
   old/new direct-MCP probes span the Helm update. Acceptance also requires the
   Ready desired-generation ActorTemplate, a fresh specialist A2A session, and
   the Coordinator composite smoke.

The relevant sanitized evidence is archived under `reports/agentic/`.
Fresh-session attestation normally uses kagent's complete actor inventory. If
a legacy actor record prevents that global list from decoding, the cutover
gate reads only the deterministic actor keys for the newly attested context
IDs from Substrate Valkey and requires the expected ActorTemplate and a
`createTime` no earlier than the smoke start. Retirement never uses this
fallback: an incomplete global inventory always blocks retirement so dormant
sessions cannot be missed.

## Rollback

If any cutover gate fails, create a commit that changes only
`activeRevision` back to the previous revision and deploy the agent release
first. Wait for its observed generation and Ready desired-generation
ActorTemplate, then run fresh specialist and Coordinator A2A smokes.

Do not roll Vault back, delete the green workload/Secret, or delete an
ActorTemplate manually. Retaining green preserves evidence and makes the
failure diagnosable.

## Retirement and purge

Retirement has a dedicated Jenkins approval. Create a commit that changes the
old inactive revision from `deploy: true` to `deploy: false`, then start the
Jenkins release with `MCP_AUTH_RETIRE_APPROVED` checked.

Before removing the old workload, the release waits until:

- Prometheus reports zero old-slot destination requests for the complete
  15-minute window;
- neither old nor active slot has a 401/5xx increase in that window;
- no non-golden live actor remains bound to a prior ActorTemplate;
- the active RemoteMCPServer, SandboxAgent generation, and ActorTemplate are
  still consistent.

The release then removes the old MCP objects, updates the specialist allowlist,
and runs fresh specialist plus Coordinator smoke. Apply Terraform after the
Jenkins retirement succeeds so Istio authorization and the retained
ExternalSecret's `deploy=false` attestation match the consumer state. Supply
the archived retirement artifact to the Terraform apply; the Terraform guard
refuses to remove the old Istio principal until the workload and old agent
allowlist are gone:

```bash
MCP_AUTH_RETIREMENT_EVIDENCE=/secure/path/mcp-auth-retirement-recommendation-....json \
terraform -chdir=infra/terraform/gcp apply
```

Purge is a later commit that removes the retired revision from the manifest.
It requires a second approval and the exact metadata-only retirement artifact:

```bash
MCP_AUTH_PURGE_APPROVED=yes \
MCP_AUTH_RETIREMENT_EVIDENCE=/secure/path/mcp-auth-retirement-recommendation-....json \
terraform -chdir=infra/terraform/gcp apply
```

Terraform refuses the purge unless the live ExternalSecret was previously
applied as `deploy=false`, the evidence identifies the same live Secret UID,
the old workload and pods are gone, and no RemoteMCPServer or SandboxAgent
still references the old Secret/domain. Only then does Helm delete the old
ExternalSecret and ESO garbage-collect its Owner-managed Secret.

## Manual diagnostic probes

These helpers emit status codes/counts and resource metadata only:

```bash
ops/validation/mcp_auth_rotation_matrix.sh recommendation v1 v2 distinct
ops/validation/mcp_auth_continuous_probe.sh recommendation v2 900
ops/validation/mcp_auth_rotation_gate.sh --help
```

Never put a token in Helm values, Jenkins parameters, command arguments,
evidence JSON, or logs.

Disabling `deploy_llm_inference` is not a rotation shortcut. Terraform refuses
that change while any managed MCP ExternalSecret or workload is live; use a
separately reviewed full-stack teardown workflow for that operation. The
`kagent` namespace is intentionally retained by a generic feature disable so
the guard cannot lose visibility of Jenkins-owned MCP resources; deleting the
namespace is an explicit final step of the teardown workflow.
