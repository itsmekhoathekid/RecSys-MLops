#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."

service="${1:?usage: printf '%s\\n' TOKEN | $0 <featureRag|recommendation> <expected-current-version>}"
expected_version="${2:?expected current Vault KV version is required}"
manifest="${MCP_AUTH_VERSIONS_FILE:-configs/agentic/mcp-auth-versions.yaml}"
mount="${MCP_AUTH_VAULT_MOUNT:-recsys}"
namespace="${KAGENT_NAMESPACE:-kagent}"
retirement_evidence="${MCP_AUTH_RETIREMENT_EVIDENCE:-}"

case "${service}" in
  featureRag) default_legacy_secret="recsys-feature-rag-mcp-auth" ;;
  recommendation) default_legacy_secret="recsys-recommendation-mcp-auth" ;;
  *)
    printf 'unsupported MCP auth service: %s\n' "${service}" >&2
    exit 2
    ;;
esac

case "$-" in
  *x*)
    printf 'refusing to rotate while shell xtrace is enabled\n' >&2
    exit 2
    ;;
esac
[[ "${expected_version}" =~ ^[1-9][0-9]*$ ]] || {
  printf 'expected current Vault version must be a positive integer\n' >&2
  exit 2
}

python3 ops/security/mcp_auth_versions.py validate "${manifest}" >/dev/null
IFS=$'\t' read -r \
  vault_path active_vault_version active_workload active_secret \
  legacy_workload legacy_secret < <(
  python3 - "${manifest}" "${service}" "${expected_version}" <<'PY'
import json
import re
import sys

manifest_path, service, expected_version = sys.argv[1:]
payload = json.load(open(manifest_path, encoding="utf-8"))
try:
    service_config = payload["services"][service]
except KeyError as exc:
    raise SystemExit(f"unknown MCP service: {service}") from exc
active_name = service_config["activeRevision"]
if re.fullmatch(r"v[1-9][0-9]*", active_name) is None:
    raise SystemExit("rotate only after migration has cut over to a versioned active revision")
active = service_config["revisions"][active_name]
declared_versions = [
    int(revision["vaultVersion"])
    for name, revision in service_config["revisions"].items()
    if name != "legacy"
]
if int(expected_version) < max(declared_versions):
    raise SystemExit("expected current Vault version predates a declared revision")
legacy = service_config["revisions"].get("legacy")
if legacy is not None and legacy["deploy"]:
    raise SystemExit("retire the mutable legacy workload before writing a new Vault version")
print(
    service_config["vaultPath"],
    active["vaultVersion"],
    active["workloadName"],
    active["secretName"],
    legacy["workloadName"] if legacy is not None else "",
    legacy["secretName"] if legacy is not None else "",
    sep="\t",
)
PY
)

# Verify the current versioned slot is actually deployed from the exact
# immutable Vault version that CAS will advance. All reads happen before the
# new token is accepted from stdin.
command -v kubectl >/dev/null 2>&1 || {
  printf 'kubectl is required to verify MCP rotation state.\n' >&2
  exit 2
}
command -v jq >/dev/null 2>&1 || {
  printf 'jq is required to verify MCP rotation state.\n' >&2
  exit 2
}
kubectl -n "${namespace}" rollout status "deployment/${active_workload}" \
  --timeout="${MCP_AUTH_ROTATE_READY_TIMEOUT:-120s}" >/dev/null
active_deployment_state="$(kubectl -n "${namespace}" get \
  "deployment.apps/${active_workload}" \
  -o jsonpath='{.spec.template.spec.containers[0].envFrom[1].secretRef.name}{"|"}{.spec.template.spec.containers[0].image}')"
IFS='|' read -r active_deployment_secret active_deployment_image \
  <<<"${active_deployment_state}"
[[ "${active_deployment_secret}" == "${active_secret}" ]] || {
  printf 'Active MCP Deployment does not consume the expected immutable Secret.\n' >&2
  exit 1
}
[[ "${active_deployment_image}" =~ @sha256:[0-9a-f]{64}$ ]] || {
  printf 'Active MCP Deployment image is not pinned by sha256 digest.\n' >&2
  exit 1
}
active_external_secret="$(kubectl -n "${namespace}" get \
  "externalsecret.external-secrets.io/${active_secret}" -o json)"
jq -e \
  --arg secret "${active_secret}" \
  --arg version "${active_vault_version}" '
    .spec.refreshPolicy == "CreatedOnce" and
    .spec.target.name == $secret and
    .spec.target.immutable == true and
    .spec.dataFrom[0].extract.version == $version and
    any(.status.conditions[]?; .type == "Ready" and .status == "True")
  ' <<<"${active_external_secret}" >/dev/null || {
  printf 'Active ExternalSecret is not Ready and pinned to active Vault version %s.\n' \
    "${active_vault_version}" >&2
  exit 1
}
active_secret_state="$(kubectl -n "${namespace}" get "secret/${active_secret}" \
  -o jsonpath='{.metadata.name}{"\t"}{.immutable}')"
[[ "${active_secret_state}" == "${active_secret}"$'\t'"true" ]] || {
  printf 'Active MCP Secret is missing or is not immutable.\n' >&2
  exit 1
}

# A purged manifest must also be purged in the cluster before another write;
# otherwise a stale mutable legacy ExternalSecret would consume the new Vault
# value. This lookup contains resource metadata/spec only, never Secret data.
live_legacy_external_secrets="$(kubectl -n "${namespace}" get \
  externalsecrets.external-secrets.io -o json)"
live_service_legacy_count="$(jq \
  --arg service "${service}" \
  --arg defaultSecret "${default_legacy_secret}" '[
    .items[]
    | select(
        .metadata.name == $defaultSecret or
        (
          .metadata.labels["recsys.ai/auth-revision"] == "legacy" and
          .metadata.annotations["recsys.ai/mcp-auth-service"] == $service
        )
      )
  ] | length' <<<"${live_legacy_external_secrets}")"
if [[ -z "${legacy_workload}" ]]; then
  ((live_service_legacy_count == 0)) || {
    printf 'Legacy ExternalSecret is still live after manifest purge; refusing Vault rotation.\n' >&2
    exit 1
  }
fi

# A deploy:false manifest is intent, not proof that Jenkins removed the old
# mutable slot. Verify the separately approved retirement and current cluster
# state before accepting credential material on stdin.
if [[ -n "${legacy_workload}" ]]; then
  [[ -n "${retirement_evidence}" && -f "${retirement_evidence}" ]] || {
    printf 'MCP_AUTH_RETIREMENT_EVIDENCE is required while legacy remains in the manifest.\n' >&2
    exit 2
  }
  jq -e \
    --arg service "${service}" \
    --arg workload "${legacy_workload}" '
      .approved == true and
      .service == $service and
      .retiringRevision == "legacy" and
      .retiringWorkload == $workload and
      .actorGate.retirement.checked == true
    ' "${retirement_evidence}" >/dev/null || {
    printf 'Retirement evidence does not attest the legacy slot for %s.\n' \
      "${service}" >&2
    exit 1
  }
  ((live_service_legacy_count == 1)) || {
    printf 'Expected exactly one retained legacy ExternalSecret for %s.\n' \
      "${service}" >&2
    exit 1
  }
  jq -e \
    --arg service "${service}" \
    --arg secret "${legacy_secret}" \
    --arg workload "${legacy_workload}" '
      any(.items[]?;
        .metadata.name == $secret and
        .metadata.annotations["recsys.ai/mcp-auth-service"] == $service and
        .metadata.annotations["recsys.ai/mcp-auth-workload"] == $workload and
        .metadata.annotations["recsys.ai/mcp-auth-deploy"] == "false")
    ' <<<"${live_legacy_external_secrets}" >/dev/null || {
    printf 'Legacy ExternalSecret has not been applied as deploy=false.\n' >&2
    exit 1
  }
  if ! legacy_deployment="$(kubectl -n "${namespace}" get \
    "deployment.apps/${legacy_workload}" --ignore-not-found -o name)"; then
    printf 'Unable to verify legacy Deployment state; refusing Vault rotation.\n' >&2
    exit 1
  fi
  [[ -z "${legacy_deployment}" ]] || {
    printf 'Legacy MCP Deployment %s is still live; refusing Vault rotation.\n' \
      "${legacy_workload}" >&2
    exit 1
  }
  if ! legacy_pods="$(kubectl -n "${namespace}" get pods \
    -l "app.kubernetes.io/name=${legacy_workload}" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')"; then
    printf 'Unable to verify legacy Pod state; refusing Vault rotation.\n' >&2
    exit 1
  fi
  [[ -z "${legacy_pods}" ]] || {
    printf 'Legacy MCP workload still has live Pods; refusing Vault rotation.\n' >&2
    exit 1
  }
fi

IFS= read -r token || true
[[ -n "${token}" ]] || {
  printf 'a non-empty token must be provided on stdin\n' >&2
  exit 2
}
if IFS= read -r _unexpected_line; then
  unset token _unexpected_line
  printf 'token input must contain exactly one line\n' >&2
  exit 2
fi

umask 077
payload_file="$(mktemp)"
result_file="$(mktemp)"
cleanup() {
  rm -f -- "${payload_file}" "${result_file}"
}
on_signal() {
  cleanup
  trap - EXIT INT TERM
  exit 130
}
trap cleanup EXIT
trap on_signal INT TERM
chmod 600 "${payload_file}" "${result_file}"

# Feed credential material only over stdin into a mode-0600 file. It never
# appears in argv, Helm values, Jenkins logs, or the checked-in manifest.
python3 -c '
import json
import sys

path = sys.argv[1]
token = sys.stdin.readline().rstrip("\r\n")
if not token:
    raise SystemExit("token is empty")
with open(path, "w", encoding="utf-8") as stream:
    json.dump(
        {"MCP_AUTH_TOKEN": token, "Authorization": "Bearer " + token},
        stream,
        separators=(",", ":"),
    )
' "${payload_file}" <<<"${token}"
unset token

vault kv put \
  -format=json \
  -mount="${mount}" \
  -cas="${expected_version}" \
  "${vault_path}" \
  "@${payload_file}" >"${result_file}"

written_version="$(python3 - "${result_file}" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
version = payload.get("data", {}).get("version")
if isinstance(version, bool) or not isinstance(version, int) or version < 1:
    raise SystemExit("Vault did not return a numeric KV version")
print(version)
PY
)"
[[ "${written_version}" -eq $((expected_version + 1)) ]] || {
  printf 'Vault returned an unexpected version after CAS write\n' >&2
  exit 1
}

# stdout is intentionally the numeric version only. Callers use this to name
# the prepare revision vN without ever handling the credential again.
printf '%s\n' "${written_version}"
