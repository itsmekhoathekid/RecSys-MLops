#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."

manifest="${1:-${MCP_AUTH_VERSIONS_FILE:-configs/agentic/mcp-auth-versions.yaml}}"
namespace="${KAGENT_NAMESPACE:-kagent}"
evidence="${MCP_AUTH_RETIREMENT_EVIDENCE:-}"
mcp_auth_enabled="${MCP_AUTH_ENABLED:-true}"

[[ "${mcp_auth_enabled}" == "true" || "${mcp_auth_enabled}" == "false" ]] || {
  printf 'MCP_AUTH_ENABLED must be true or false.\n' >&2
  exit 2
}

python3 ops/security/mcp_auth_versions.py validate "${manifest}" >/dev/null
command -v kubectl >/dev/null 2>&1 || {
  printf 'kubectl is required for Terraform MCP auth purge validation.\n' >&2
  exit 2
}
command -v jq >/dev/null 2>&1 || {
  printf 'jq is required for Terraform MCP auth purge validation.\n' >&2
  exit 2
}

desired_records="$(python3 - "${manifest}" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
records = [
    {
        "secret": revision["secretName"],
        "service": service_name,
        "revision": revision_name,
        "workload": revision["workloadName"],
        "deploy": revision["deploy"],
    }
    for service_name, service in payload["services"].items()
    for revision_name, revision in service["revisions"].items()
]
print(json.dumps(sorted(records, key=lambda item: item["secret"]), sort_keys=True))
PY
)"

namespace_exists=false
external_secret_crd_exists=false
if ! namespace_resource="$(kubectl get "namespace/${namespace}" \
  --ignore-not-found -o name)"; then
  printf 'Unable to verify whether namespace %s exists; refusing Terraform MCP auth validation.\n' \
    "${namespace}" >&2
  exit 1
fi
if ! external_secret_crd="$(kubectl get \
  'customresourcedefinition/externalsecrets.external-secrets.io' \
  --ignore-not-found -o name)"; then
  printf 'Unable to verify ExternalSecret CRD state; refusing Terraform MCP auth validation.\n' >&2
  exit 1
fi
if [[ "${external_secret_crd}" == \
  "customresourcedefinition.apiextensions.k8s.io/externalsecrets.external-secrets.io" ]]; then
  external_secret_crd_exists=true
elif [[ -n "${external_secret_crd}" ]]; then
  printf 'Unexpected ExternalSecret CRD lookup result; refusing Terraform MCP auth validation.\n' >&2
  exit 1
fi
if [[ "${namespace_resource}" == "namespace/${namespace}" ]]; then
  namespace_exists=true
  if [[ "${external_secret_crd_exists}" == "true" ]]; then
    live="$(kubectl -n "${namespace}" get externalsecrets.external-secrets.io -o json)"
  elif [[ "${mcp_auth_enabled}" == "true" ]]; then
    printf 'ExternalSecret CRD is required while MCP auth management is enabled.\n' >&2
    exit 1
  else
    live='{"items":[]}'
  fi
elif [[ -z "${namespace_resource}" ]]; then
  live='{"items":[]}'
else
  printf 'Unexpected namespace lookup result for %s; refusing Terraform MCP auth validation.\n' \
    "${namespace}" >&2
  exit 1
fi
live="$(jq -c '
  [.items[]
   | select(
       (.metadata.labels["recsys.ai/auth-revision"] // "") != "" or
       .metadata.name == "recsys-feature-rag-mcp-auth" or
       .metadata.name == "recsys-recommendation-mcp-auth" or
       (.metadata.name | test("^recsys-(feature-rag|recommendation)-mcp-auth-v[1-9][0-9]*$"))
     )]
  | {items: .}
' <<<"${live}")"

if [[ "${mcp_auth_enabled}" == "false" ]]; then
  live_external_secret_count="$(jq '.items | length' <<<"${live}")"
  live_workload_count=0
  if [[ "${namespace_exists}" == "true" ]]; then
    live_workload_count="$(kubectl -n "${namespace}" get deployments.apps -o json \
      | jq --argjson desired "${desired_records}" '[
          .items[]
          | .metadata.name as $name
          | select(
              ([$desired[].workload] | index($name)) != null or
              .metadata.labels["app.kubernetes.io/component"] == "mcp-server" or
              .metadata.labels["app.kubernetes.io/component"] == "recommendation-mcp"
            )
        ] | length')"
  fi
  if ((live_external_secret_count > 0 || live_workload_count > 0)); then
    printf 'Refusing to disable MCP auth management while %s ExternalSecret(s) and %s MCP workload(s) are live; use a separately reviewed full-stack teardown workflow.\n' \
      "${live_external_secret_count}" "${live_workload_count}" >&2
    exit 2
  fi
  jq -n --arg checkedAt "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{checkedAt: $checkedAt, manifestValid: true, mcpAuthEnabled: false, retirementCandidates: 0, purgeCandidates: 0}'
  exit 0
fi
purge_records=()
retirement_records=()
while IFS=$'\t' read -r secret service revision workload deploy; do
  [[ -n "${secret}" ]] || continue
  desired_record="$(jq -c --arg name "${secret}" \
    '[.[] | select(.secret == $name)] | if length == 1 then .[0] else empty end' \
    <<<"${desired_records}")"
  if [[ -n "${desired_record}" ]]; then
    desired_deploy="$(jq -r '.deploy' <<<"${desired_record}")"
    if [[ "${desired_deploy}" == "false" && "${deploy}" != "false" ]]; then
      retirement_records+=("$(jq -r \
        '[.secret, .service, .revision, .workload] | @tsv' \
        <<<"${desired_record}")"$'\t'"${deploy}")
    fi
    continue
  fi
  purge_records+=("${secret}"$'\t'"${service}"$'\t'"${revision}"$'\t'"${workload}"$'\t'"${deploy}")
done < <(jq -r '.items[] | [
  (.spec.target.name // .metadata.name),
  (.metadata.annotations["recsys.ai/mcp-auth-service"] // ""),
  (.metadata.labels["recsys.ai/auth-revision"] // ""),
  (.metadata.annotations["recsys.ai/mcp-auth-workload"] // ""),
  (.metadata.annotations["recsys.ai/mcp-auth-deploy"] // "")
] | @tsv' <<<"${live}")

if ((${#purge_records[@]} == 0 && ${#retirement_records[@]} == 0)); then
  jq -n --arg checkedAt "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{checkedAt: $checkedAt, manifestValid: true, retirementCandidates: 0, purgeCandidates: 0}'
  exit 0
fi

if ((${#purge_records[@]} > 0 && ${#retirement_records[@]} > 0)); then
  printf 'Retirement and purge cannot be applied in the same Terraform transaction.\n' >&2
  exit 2
fi
if ((${#purge_records[@]} > 1 || ${#retirement_records[@]} > 1)); then
  printf 'Exactly one MCP auth revision may retire or purge per Terraform apply.\n' >&2
  exit 2
fi

operation=retirement
record="${retirement_records[0]:-}"
if ((${#purge_records[@]} == 1)); then
  operation=purge
  record="${purge_records[0]}"
  [[ "${MCP_AUTH_PURGE_APPROVED:-}" == "yes" ]] || {
    printf 'MCP auth Secret purge detected; set MCP_AUTH_PURGE_APPROVED=yes after the separate purge review.\n' >&2
    exit 2
  }
fi
[[ -n "${evidence}" && -f "${evidence}" ]] || {
  printf 'MCP auth %s apply detected; MCP_AUTH_RETIREMENT_EVIDENCE must name the reviewed Jenkins retirement JSON.\n' \
    "${operation}" >&2
  exit 2
}

IFS=$'\t' read -r secret service revision workload deploy <<<"${record}"
[[ -n "${service}" && -n "${revision}" && -n "${workload}" ]] || {
  printf 'Live ExternalSecret lacks reviewed rotation annotations; refusing %s.\n' \
    "${operation}" >&2
  exit 1
}
if [[ "${operation}" == "purge" && "${deploy}" != "false" ]]; then
  printf 'Revision %s/%s was not applied as deploy=false before purge.\n' \
    "${service}" "${revision}" >&2
  exit 1
fi

jq -e \
  --arg service "${service}" \
  --arg revision "${revision}" \
  --arg workload "${workload}" \
  --arg secret "${secret}" '
    .approved == true and
    .service == $service and
    .retiringRevision == $revision and
    .retiringWorkload == $workload and
    .retiringSecret.name == $secret and
    .retiringSecret.uid != "" and
    .retiringSecret.resourceVersion != "" and
    .traffic.oldSlotRequests == 0 and
    .traffic.authOr5xxErrors == 0 and
    .traffic.telemetry.currentWorkloads == 2 and
    .traffic.telemetry.windowStartWorkloads == 2 and
    .traffic.telemetry.minimumSamplesObserved >=
      .traffic.telemetry.minimumSamplesRequired and
    .traffic.telemetry.maximumAgeSecondsObserved <=
      .traffic.telemetry.maximumAgeSecondsAllowed and
    .actorGate.retirement.checked == true
  ' "${evidence}" >/dev/null || {
  printf 'Retirement evidence does not attest this exact %s target.\n' \
    "${operation}" >&2
  exit 1
}

live_secret_uid="$(kubectl -n "${namespace}" get "secret/${secret}" \
  -o jsonpath='{.metadata.uid}')"
live_secret_resource_version="$(kubectl -n "${namespace}" get "secret/${secret}" \
  -o jsonpath='{.metadata.resourceVersion}')"
evidence_secret_uid="$(jq -r '.retiringSecret.uid' "${evidence}")"
evidence_secret_resource_version="$(jq -r \
  '.retiringSecret.resourceVersion' "${evidence}")"
[[ "${live_secret_uid}" == "${evidence_secret_uid}" ]] || {
  printf 'Secret identity changed after retirement evidence; refusing %s.\n' \
    "${operation}" >&2
  exit 1
}
[[ -n "${evidence_secret_resource_version}" &&
      "${live_secret_resource_version}" == "${evidence_secret_resource_version}" ]] || {
  printf 'Secret resourceVersion changed after retirement evidence; refusing %s.\n' \
    "${operation}" >&2
  exit 1
}
live_external_secret_uid="$(kubectl -n "${namespace}" get \
  "externalsecret.external-secrets.io/${secret}" -o jsonpath='{.metadata.uid}')"
evidence_external_secret_uid="$(jq -r '.retiringSecret.externalSecretUid' "${evidence}")"
[[ -n "${evidence_external_secret_uid}" &&
      "${live_external_secret_uid}" == "${evidence_external_secret_uid}" ]] || {
  printf 'ExternalSecret identity changed after retirement evidence; refusing %s.\n' \
    "${operation}" >&2
  exit 1
}

for resource in deployment service configmap serviceaccount \
  poddisruptionbudget.policy scaledobject.keda.sh networkpolicy.networking.k8s.io \
  servicemonitor.monitoring.coreos.com; do
  if ! live_resource="$(kubectl -n "${namespace}" get \
    "${resource}/${workload}" --ignore-not-found -o name)"; then
    printf 'Unable to verify retired resource state for %s/%s.\n' \
      "${resource}" "${workload}" >&2
    exit 1
  fi
  if [[ -n "${live_resource}" ]]; then
    printf 'Retired resource still exists: %s/%s.\n' "${resource}" "${workload}" >&2
    exit 1
  fi
done
remaining_pods="$(kubectl -n "${namespace}" get pods \
  -l "app.kubernetes.io/name=${workload}" -o json | jq '.items | length')"
((remaining_pods == 0)) || {
  printf 'Retired workload still has %s pod(s).\n' "${remaining_pods}" >&2
  exit 1
}

remote_refs="$(kubectl -n "${namespace}" get remotemcpservers.kagent.dev -o json \
  | jq --arg secret "${secret}" '[.items[].spec.headersFrom[]?.valueFrom.name | select(. == $secret)] | length')"
((remote_refs == 0)) || {
  printf 'A RemoteMCPServer still references retired Secret %s.\n' "${secret}" >&2
  exit 1
}
domain="${workload}.${namespace}.svc.cluster.local"
sandbox_refs="$(kubectl -n "${namespace}" get sandboxagents.kagent.dev -o json \
  | jq --arg domain "${domain}" '[.items[].spec.sandbox.network.allowedDomains[]? | select(. == $domain)] | length')"
((sandbox_refs == 0)) || {
  printf 'A SandboxAgent still allows retired workload domain %s.\n' "${domain}" >&2
  exit 1
}

jq -n \
  --arg checkedAt "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  --arg operation "${operation}" \
  --arg service "${service}" \
  --arg revision "${revision}" \
  --arg workload "${workload}" \
  --arg secret "${secret}" \
  --arg secretUid "${live_secret_uid}" \
  --arg externalSecretUid "${live_external_secret_uid}" \
  '{
    checkedAt: $checkedAt,
    manifestValid: true,
    retirementCandidates: (if $operation == "retirement" then 1 else 0 end),
    purgeCandidates: (if $operation == "purge" then 1 else 0 end),
    approvedTarget: {
      operation: $operation,
      service: $service,
      revision: $revision,
      workload: $workload,
      secret: $secret,
      secretUid: $secretUid,
      externalSecretUid: $externalSecretUid
    }
  }'
