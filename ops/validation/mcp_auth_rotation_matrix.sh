#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
source jenkins/scripts/deploy/agentic/rotation.sh

namespace="${KAGENT_NAMESPACE:-kagent}"
service="${1:?usage: $0 <featureRag|recommendation> <old-revision> <new-revision> [same|distinct]}"
old_revision="${2:?old revision is required}"
new_revision="${3:?new revision is required}"
credential_mode="${4:-distinct}"
evidence_path="${MCP_AUTH_MATRIX_EVIDENCE:-}"

[[ "${credential_mode}" == "same" || "${credential_mode}" == "distinct" ]] || {
  printf 'credential mode must be same or distinct\n' >&2
  exit 2
}

mcp_auth_validate

revision_record() {
  local wanted="$1"
  local revision secret_name workload_name vault_version
  while IFS=$'\t' read -r revision secret_name workload_name vault_version; do
    if [[ "${revision}" == "${wanted}" ]]; then
      printf '%s\t%s\t%s\t%s\n' \
        "${revision}" "${secret_name}" "${workload_name}" "${vault_version}"
      return 0
    fi
  done < <(mcp_auth_list_deployed "${service}")
  printf 'revision %s is not deploy:true for %s\n' "${wanted}" "${service}" >&2
  return 2
}

IFS=$'\t' read -r _ old_secret old_workload old_vault_version \
  < <(revision_record "${old_revision}")
IFS=$'\t' read -r _ new_secret new_workload new_vault_version \
  < <(revision_record "${new_revision}")

for workload in "${old_workload}" "${new_workload}"; do
  kubectl -n "${namespace}" rollout status "deployment/${workload}" \
    --timeout="${MCP_AUTH_MATRIX_TIMEOUT:-600s}"
done

probe() {
  local source_workload="$1"
  local target_workload="$2"
  local auth_mode="$3"
  local expected_status="$4"
  local label="$5"
  kubectl -n "${namespace}" exec "deployment/${source_workload}" -c mcp \
    -- env \
      "MCP_ROTATION_TARGET=http://${target_workload}.${namespace}.svc.cluster.local:8080/mcp" \
      "MCP_ROTATION_AUTH_MODE=${auth_mode}" \
      "MCP_ROTATION_EXPECTED_STATUS=${expected_status}" \
      "MCP_ROTATION_LABEL=${label}" \
      python -c '
import json
import os

import httpx

mode = os.environ["MCP_ROTATION_AUTH_MODE"]
headers = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
if mode == "own":
    headers["Authorization"] = "Bearer " + os.environ["MCP_AUTH_TOKEN"]
elif mode == "invalid":
    headers["Authorization"] = "Bearer intentionally-invalid-rotation-probe"
elif mode != "missing":
    raise SystemExit("unsupported auth mode")

payload = {
    "jsonrpc": "2.0",
    "id": "auth-matrix",
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "mcp-auth-rotation", "version": "1"},
    },
}
response = httpx.post(
    os.environ["MCP_ROTATION_TARGET"],
    content=json.dumps(payload),
    headers=headers,
    timeout=30,
)
status = response.status_code
print("{} status={}".format(os.environ["MCP_ROTATION_LABEL"], status))
if status != int(os.environ["MCP_ROTATION_EXPECTED_STATUS"]):
    raise SystemExit(1)
'
}

cross_status=401
[[ "${credential_mode}" == "same" ]] && cross_status=200

# Tokens remain inside their source pods. Only labels and HTTP status codes are
# emitted; neither Secret data nor response bodies cross kubectl exec.
probe "${old_workload}" "${old_workload}" own 200 old-token_to_blue
probe "${new_workload}" "${new_workload}" own 200 new-token_to_green
probe "${old_workload}" "${new_workload}" own "${cross_status}" old-token_to_green
probe "${new_workload}" "${old_workload}" own "${cross_status}" new-token_to_blue
probe "${new_workload}" "${new_workload}" missing 401 missing-token_to_green
probe "${new_workload}" "${new_workload}" invalid 401 invalid-token_to_green

if [[ -n "${evidence_path}" ]]; then
  [[ ! -e "${evidence_path}" ]] || {
    printf 'Refusing to overwrite auth-matrix evidence: %s\n' "${evidence_path}" >&2
    exit 2
  }
  mkdir -p "$(dirname "${evidence_path}")"
  resource_metadata() {
    local resource="$1"
    local name="$2"
    local resource_name uid generation resource_version
    IFS='|' read -r resource_name uid generation resource_version < <(
      kubectl -n "${namespace}" get "${resource}/${name}" \
        -o jsonpath='{.metadata.name}{"|"}{.metadata.uid}{"|"}{.metadata.generation}{"|"}{.metadata.resourceVersion}{"\n"}'
    )
    [[ -n "${resource_name}" && -n "${uid}" && -n "${resource_version}" ]] || {
      printf 'Incomplete metadata for %s/%s\n' "${resource}" "${name}" >&2
      return 1
    }
    jq -cn \
      --arg name "${resource_name}" \
      --arg uid "${uid}" \
      --arg generation "${generation}" \
      --arg resourceVersion "${resource_version}" \
      '{
        name: $name,
        uid: $uid,
        generation: (if $generation == "" then null else ($generation | tonumber) end),
        resourceVersion: $resourceVersion
      }'
  }
  old_deployment="$(resource_metadata deployment "${old_workload}")"
  new_deployment="$(resource_metadata deployment "${new_workload}")"
  old_secret_metadata="$(resource_metadata secret "${old_secret}")"
  new_secret_metadata="$(resource_metadata secret "${new_secret}")"
  old_external_secret="$(resource_metadata externalsecret "${old_secret}")"
  new_external_secret="$(resource_metadata externalsecret "${new_secret}")"
  jq -n \
    --arg checkedAt "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    --arg service "${service}" \
    --arg oldRevision "${old_revision}" \
    --arg newRevision "${new_revision}" \
    --arg oldVaultVersion "${old_vault_version}" \
    --arg newVaultVersion "${new_vault_version}" \
    --arg credentialMode "${credential_mode}" \
    --argjson crossStatus "${cross_status}" \
    --argjson oldDeployment "${old_deployment}" \
    --argjson newDeployment "${new_deployment}" \
    --argjson oldSecret "${old_secret_metadata}" \
    --argjson newSecret "${new_secret_metadata}" \
    --argjson oldExternalSecret "${old_external_secret}" \
    --argjson newExternalSecret "${new_external_secret}" \
    '{
      checkedAt: $checkedAt,
      service: $service,
      credentialMode: $credentialMode,
      old: {
        revision: $oldRevision,
        vaultVersion: $oldVaultVersion,
        deployment: $oldDeployment,
        secret: $oldSecret,
        externalSecret: $oldExternalSecret
      },
      new: {
        revision: $newRevision,
        vaultVersion: $newVaultVersion,
        deployment: $newDeployment,
        secret: $newSecret,
        externalSecret: $newExternalSecret
      },
      results: [
        {case: "old-token_to_blue", status: 200},
        {case: "new-token_to_green", status: 200},
        {case: "old-token_to_green", status: $crossStatus},
        {case: "new-token_to_blue", status: $crossStatus},
        {case: "missing-token_to_green", status: 401},
        {case: "invalid-token_to_green", status: 401}
      ]
    }' >"${evidence_path}"
fi

printf 'MCP auth matrix passed service=%s old=%s new=%s mode=%s evidence=%s\n' \
  "${service}" "${old_workload}" "${new_workload}" "${credential_mode}" \
  "${evidence_path:-none}"
