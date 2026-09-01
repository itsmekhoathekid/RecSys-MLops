#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_JSON="${PUBLIC_AGENT_SURFACES_JSON:-${ROOT_DIR}/reports/gcp/public-agent-surfaces.json}"
EXPECTED_IP="${PUBLIC_AGENT_SURFACES_IP:-136.85.106.59}"
AUTH_USER="${GCP_CHECK_BASIC_AUTH_USER:-${GATEWAY_AUTH_USER:-}}"
AUTH_PASSWORD="${GCP_CHECK_BASIC_AUTH_PASSWORD:-${GATEWAY_AUTH_PASSWORD:-}}"
RESULTS_FILE="$(mktemp)"
NETRC_FILE="$(mktemp)"
trap 'rm -f "${RESULTS_FILE}" "${NETRC_FILE}"' EXIT

record() {
  local id="$1" status="$2" description="$3" detail="${4:-}"
  detail="${detail//$'\t'/ }"
  detail="${detail//$'\n'/ }"
  printf '%s\t%s\t%s\t%s\n' "${id}" "${status}" "${description}" "${detail}" >>"${RESULTS_FILE}"
  printf '%-6s %-32s %s\n' "${status}" "${id}" "${description}"
}

check_cmd() {
  local id="$1" description="$2"
  shift 2
  local output_file
  output_file="$(mktemp)"
  if "$@" >"${output_file}" 2>&1; then
    record "${id}" PASS "${description}" "$(head -n 1 "${output_file}")"
  else
    record "${id}" FAIL "${description}" "$(head -n 1 "${output_file}")"
  fi
  rm -f "${output_file}"
}

check_status() {
  local id="$1" description="$2" url="$3" expected="$4" mode="${5:-authenticated}"
  local status host
  host="${url#https://}"
  host="${host%%/*}"
  if [[ "${mode}" == "authenticated" ]]; then
    status="$(curl -sS --resolve "${host}:443:${EXPECTED_IP}" --netrc-file "${NETRC_FILE}" --max-time 20 -o /dev/null -w '%{http_code}' "${url}" || true)"
  else
    status="$(curl -sS --resolve "${host}:443:${EXPECTED_IP}" --max-time 20 -o /dev/null -w '%{http_code}' "${url}" || true)"
  fi
  if [[ ",${expected}," == *",${status},"* ]]; then
    record "${id}" PASS "${description}" "HTTP ${status}"
  else
    record "${id}" FAIL "${description}" "expected HTTP ${expected}, got ${status:-unreachable}"
  fi
}

if [[ -z "${AUTH_USER}" || -z "${AUTH_PASSWORD}" ]]; then
  echo "GCP_CHECK_BASIC_AUTH_USER and GCP_CHECK_BASIC_AUTH_PASSWORD are required" >&2
  exit 2
fi

chmod 600 "${NETRC_FILE}"
for host in agents.recsys-mlops.site registry.recsys-mlops.site rag.recsys-mlops.site; do
  printf 'machine %s login %s password %s\n' "${host}" "${AUTH_USER}" "${AUTH_PASSWORD}" >>"${NETRC_FILE}"
done

for host in agents.recsys-mlops.site registry.recsys-mlops.site rag.recsys-mlops.site; do
  id="${host%%.*}"
  auth_path="/"
  [[ "${id}" == "rag" ]] && auth_path="/docs"
  check_cmd "dns-${id}" "${host} resolves to the production ingress" \
    bash -c '[[ "$(dig +short "$1" A | tail -n 1)" == "$2" ]]' _ "${host}" "${EXPECTED_IP}"
  check_status "auth-${id}" "${host} rejects missing Basic Auth" "https://${host}${auth_path}" "401" unauthenticated
done

check_cmd auth-challenge "Gateway returns a Basic Auth challenge" \
  bash -c 'curl -sSI --resolve agents.recsys-mlops.site:443:'"${EXPECTED_IP}"' --max-time 20 https://agents.recsys-mlops.site/ | tr -d "\r" | grep -qi "^www-authenticate: basic"'

check_status agents-ui "Agent test UI is reachable" "https://agents.recsys-mlops.site/" "200"
check_status agents-health "Agent UI same-origin proxy health is reachable" "https://agents.recsys-mlops.site/health" "200"
check_status registry-ui "Agent Registry UI is reachable" "https://registry.recsys-mlops.site/" "200"
check_status rag-root "RAG exact root redirects to Swagger" "https://rag.recsys-mlops.site/" "301,302,308"
check_status rag-docs "RAG Swagger UI is reachable" "https://rag.recsys-mlops.site/docs" "200"
check_status rag-openapi "RAG OpenAPI schema is reachable" "https://rag.recsys-mlops.site/openapi.json" "200"

for private_path in metrics healthz ready version; do
  check_status "rag-private-${private_path}" "RAG /${private_path} remains private" \
    "https://rag.recsys-mlops.site/${private_path}" "404"
done

check_cmd k8s-auth-secrets "Basic Auth secret exists in both UI namespaces" \
  bash -c 'kubectl get secret recsys-gateway-basic-auth -n kagent >/dev/null && kubectl get secret recsys-gateway-basic-auth -n agentregistry >/dev/null'
check_cmd k8s-certificates "All three public certificates are Ready" \
  bash -c 'kubectl get certificate -A -o json | jq -e '\''[.items[] | select(.metadata.name == "recsys-agents-tls" or .metadata.name == "recsys-registry-tls" or .metadata.name == "recsys-rag-tls")] | length == 3 and all(.[]; any(.status.conditions[]?; .type == "Ready" and .status == "True"))'\'''
check_cmd k8s-ingresses "All four new ingress resources use the production address" \
  bash -c 'kubectl get ingress -A -o json | jq -e '\''[.items[] | select(.metadata.name == "recsys-agent-test-ui-gateway" or .metadata.name == "recsys-agent-registry-gateway" or .metadata.name == "recsys-rag-api-gateway" or .metadata.name == "recsys-rag-api-root-redirect")] | length == 4 and all(.[]; .status.loadBalancer.ingress[0].ip == "'"${EXPECTED_IP}"'")'\'''

mkdir -p "$(dirname "${OUTPUT_JSON}")"
jq -Rn '[inputs | split("\t") | {id: .[0], status: .[1], description: .[2], detail: .[3]}]' \
  <"${RESULTS_FILE}" >"${OUTPUT_JSON}"
failures="$(jq '[.[] | select(.status == "FAIL")] | length' "${OUTPUT_JSON}")"
passes="$(jq '[.[] | select(.status == "PASS")] | length' "${OUTPUT_JSON}")"
printf '\nSummary: PASS=%s FAIL=%s\nJSON: %s\n' "${passes}" "${failures}" "${OUTPUT_JSON}"
[[ "${failures}" == 0 ]]
