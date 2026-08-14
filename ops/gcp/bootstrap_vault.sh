#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
artifact_dir="${repo_root}/.vault-bootstrap"
artifact_file="${artifact_dir}/vault-init.json.enc"
vault_namespace="vault"
vault_pod="vault-0"
vault_addr="http://127.0.0.1:8200"
kms_location="${VAULT_KMS_LOCATION:-global}"
kms_keyring="${VAULT_KMS_KEYRING:-recsys-mlops-vault}"
kms_key="${VAULT_KMS_KEY:-vault-unseal}"
gcp_project="${VAULT_GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"

service_secret_groups=(data-platform mlflow runtime kserve-minio gateway analytics jenkins-runtime agent-gateway agentregistry)

for required_command in gcloud jq kubectl openssl; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "Required command not found: ${required_command}" >&2
    exit 1
  fi
done

if [[ -z "${gcp_project}" || "${gcp_project}" == "(unset)" ]]; then
  echo "Set VAULT_GCP_PROJECT or select a gcloud project before running this script." >&2
  exit 1
fi

umask 077
mkdir -p "${artifact_dir}"
bootstrap_tmp="$(mktemp -d "${TMPDIR:-/tmp}/recsys-vault-bootstrap.XXXXXX")"
trap 'rm -rf "${bootstrap_tmp}"' EXIT

vault_exec() {
  local token="$1"
  shift
  printf '%s\n' "${token}" | kubectl exec -i -n "${vault_namespace}" "${vault_pod}" -- \
    sh -c 'IFS= read -r VAULT_TOKEN; export VAULT_TOKEN; export VAULT_ADDR="$1"; shift; exec vault "$@"' \
    sh "${vault_addr}" "$@"
}

vault_exec_with_payload() {
  local token="$1"
  local payload_file="$2"
  shift 2
  {
    printf '%s\n' "${token}"
    sed -n '1,$p' "${payload_file}"
  } | kubectl exec -i -n "${vault_namespace}" "${vault_pod}" -- \
    sh -c 'IFS= read -r VAULT_TOKEN; export VAULT_TOKEN; export VAULT_ADDR="$1"; shift; exec vault "$@"' \
    sh "${vault_addr}" "$@"
}

encrypt_bootstrap_file() {
  local plaintext_file="$1"
  local ciphertext_file="$2"
  gcloud kms encrypt \
    --project "${gcp_project}" \
    --location "${kms_location}" \
    --keyring "${kms_keyring}" \
    --key "${kms_key}" \
    --plaintext-file "${plaintext_file}" \
    --ciphertext-file "${ciphertext_file}" \
    --quiet
}

decrypt_bootstrap_file() {
  local ciphertext_file="$1"
  local plaintext_file="$2"
  gcloud kms decrypt \
    --project "${gcp_project}" \
    --location "${kms_location}" \
    --keyring "${kms_keyring}" \
    --key "${kms_key}" \
    --ciphertext-file "${ciphertext_file}" \
    --plaintext-file "${plaintext_file}" \
    --quiet
}

echo "Checking Cloud KMS encryption before Vault initialization..."
printf '%s\n' "recsys-vault-bootstrap-preflight" >"${bootstrap_tmp}/kms-plain"
encrypt_bootstrap_file "${bootstrap_tmp}/kms-plain" "${bootstrap_tmp}/kms-cipher"
decrypt_bootstrap_file "${bootstrap_tmp}/kms-cipher" "${bootstrap_tmp}/kms-roundtrip"
cmp "${bootstrap_tmp}/kms-plain" "${bootstrap_tmp}/kms-roundtrip" >/dev/null

echo "Waiting for ${vault_namespace}/${vault_pod}..."
kubectl wait --for=condition=PodScheduled "pod/${vault_pod}" \
  -n "${vault_namespace}" --timeout=300s >/dev/null

vault_status_file="${bootstrap_tmp}/vault-status.json"
for _ in $(seq 1 60); do
  if kubectl exec -n "${vault_namespace}" "${vault_pod}" -- \
    env VAULT_ADDR="${vault_addr}" vault status -format=json >"${vault_status_file}" 2>/dev/null; then
    break
  fi
  if [[ -s "${vault_status_file}" ]] && jq -e '.initialized == false or .sealed == true' "${vault_status_file}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if [[ ! -s "${vault_status_file}" ]]; then
  echo "Vault API did not become reachable." >&2
  exit 1
fi

initialized="$(jq -r '.initialized' "${vault_status_file}")"
bootstrap_plain="${bootstrap_tmp}/vault-bootstrap.json"
initial_root_token=""
admin_token=""

if [[ "${initialized}" == "false" ]]; then
  echo "Initializing Vault with GCP Cloud KMS auto-unseal..."
  kubectl exec -n "${vault_namespace}" "${vault_pod}" -- \
    env VAULT_ADDR="${vault_addr}" vault operator init \
      -format=json \
      -recovery-shares=5 \
      -recovery-threshold=3 >"${bootstrap_plain}"

  # Persist an encrypted recovery copy immediately. No plaintext key material
  # is written outside the private temporary directory.
  encrypt_bootstrap_file "${bootstrap_plain}" "${artifact_file}"
  initial_root_token="$(jq -er '.root_token' "${bootstrap_plain}")"
  active_token="${initial_root_token}"
else
  if [[ ! -f "${artifact_file}" ]]; then
    echo "Vault is initialized but ${artifact_file} is missing; refusing to continue without recoverable credentials." >&2
    exit 1
  fi
  decrypt_bootstrap_file "${artifact_file}" "${bootstrap_plain}"
  admin_token="$(jq -er '.recsys_admin_token' "${bootstrap_plain}")"
  active_token="${admin_token}"
  echo "Vault is already initialized; using the KMS-encrypted scoped admin token."
fi

echo "Waiting for Vault auto-unseal..."
for _ in $(seq 1 60); do
  if vault_exec "${active_token}" status -format=json 2>/dev/null | jq -e '.initialized == true and .sealed == false' >/dev/null; then
    break
  fi
  sleep 2
done
vault_exec "${active_token}" status -format=json | jq '{initialized, sealed, storage_type, ha_enabled}'

if ! vault_exec "${active_token}" secrets list -format=json | jq -e 'has("recsys/")' >/dev/null; then
  vault_exec "${active_token}" secrets enable -path=recsys -version=2 kv >/dev/null
fi

external_secrets_policy="${bootstrap_tmp}/external-secrets-policy.hcl"
printf '%s\n' \
  'path "recsys/data/*" {' \
  '  capabilities = ["read"]' \
  '}' \
  'path "recsys/metadata/*" {' \
  '  capabilities = ["read", "list"]' \
  '}' >"${external_secrets_policy}"
vault_exec_with_payload "${active_token}" "${external_secrets_policy}" \
  policy write recsys-external-secrets - >/dev/null

if ! vault_exec "${active_token}" auth list -format=json | jq -e 'has("kubernetes/")' >/dev/null; then
  vault_exec "${active_token}" auth enable kubernetes >/dev/null
fi

vault_exec "${active_token}" write auth/kubernetes/config \
  kubernetes_host="https://kubernetes.default.svc" >/dev/null
vault_exec "${active_token}" write auth/kubernetes/role/recsys-external-secrets \
  bound_service_account_names="external-secrets" \
  bound_service_account_namespaces="external-secrets" \
  audience="vault" \
  policies="recsys-external-secrets" \
  token_ttl="1h" \
  token_max_ttl="4h" >/dev/null

echo "Copying service secret groups into Vault KV v2 without printing values..."
for secret_group in "${service_secret_groups[@]}"; do
  if ! kubectl get secret "${secret_group}" -n external-secrets >/dev/null 2>&1; then
    if [[ "${secret_group}" == "agent-gateway" ]]; then
      if vault_exec "${active_token}" kv metadata get -mount=recsys "${secret_group}" >/dev/null 2>&1; then
        echo "  ${secret_group}: legacy source absent; keeping the existing Vault version"
      else
        payload_file="${bootstrap_tmp}/${secret_group}.json"
        agent_gateway_api_key="agw-$(openssl rand -hex 32)"
        jq -n --arg api_key "${agent_gateway_api_key}" \
          '{data: {AGENT_GATEWAY_API_KEY: $api_key}}' >"${payload_file}"
        unset agent_gateway_api_key
        vault_exec_with_payload "${active_token}" "${payload_file}" \
          write "recsys/data/${secret_group}" - >/dev/null
        echo "  ${secret_group}: generated and stored 1 key"
      fi
      continue
    fi
    if [[ "${secret_group}" == "agentregistry" ]]; then
      if vault_exec "${active_token}" kv metadata get -mount=recsys "${secret_group}" >/dev/null 2>&1; then
        echo "  ${secret_group}: legacy source absent; keeping the existing Vault version"
      else
        payload_file="${bootstrap_tmp}/${secret_group}.json"
        postgres_password="$(openssl rand -hex 32)"
        database_url="postgresql://agentregistry:${postgres_password}@agentregistry-postgres.agentregistry.svc.cluster.local:5432/agentregistry?sslmode=disable"
        jq -n \
          --arg database "agentregistry" \
          --arg username "agentregistry" \
          --arg password "${postgres_password}" \
          --arg database_url "${database_url}" \
          '{data: {POSTGRES_DB: $database, POSTGRES_USER: $username, POSTGRES_PASSWORD: $password, AGENT_REGISTRY_DATABASE_URL: $database_url}}' \
          >"${payload_file}"
        unset postgres_password database_url
        vault_exec_with_payload "${active_token}" "${payload_file}" \
          write "recsys/data/${secret_group}" - >/dev/null
        echo "  ${secret_group}: generated and stored 4 keys"
      fi
      continue
    fi
    echo "  ${secret_group}: legacy source absent; keeping the existing Vault version"
    continue
  fi
  payload_file="${bootstrap_tmp}/${secret_group}.json"
  kubectl get secret "${secret_group}" -n external-secrets -o json | \
    jq '{data: (.data | with_entries(.value |= @base64d))}' >"${payload_file}"
  vault_exec_with_payload "${active_token}" "${payload_file}" \
    write "recsys/data/${secret_group}" - >/dev/null
  key_count="$(jq '.data | length' "${payload_file}")"
  echo "  ${secret_group}: ${key_count} keys stored"
done

for secret_group in "${service_secret_groups[@]}"; do
  vault_exec "${active_token}" kv metadata get -format=json -mount=recsys "${secret_group}" | \
    jq -e '.data.current_version >= 1' >/dev/null
done

if [[ -n "${initial_root_token}" ]]; then
  # First initialization only: create a narrowly scoped policy for future
  # secret administration. The initial root token is used only to bootstrap
  # this policy and the token below; it is revoked before the script exits.
  admin_policy="${bootstrap_tmp}/admin-policy.hcl"
  printf '%s\n' \
    'path "recsys/*" {' \
    '  capabilities = ["create", "read", "update", "patch", "delete", "list"]' \
    '}' \
    'path "sys/mounts" {' \
    '  capabilities = ["read"]' \
    '}' \
    'path "sys/auth" {' \
    '  capabilities = ["read"]' \
    '}' \
    'path "sys/policies/acl/recsys-external-secrets" {' \
    '  capabilities = ["create", "read", "update"]' \
    '}' \
    'path "auth/kubernetes/config" {' \
    '  capabilities = ["create", "read", "update"]' \
    '}' \
    'path "auth/kubernetes/role/recsys-external-secrets" {' \
    '  capabilities = ["create", "read", "update"]' \
    '}' >"${admin_policy}"
  vault_exec_with_payload "${initial_root_token}" "${admin_policy}" \
    policy write recsys-secrets-admin - >/dev/null

  # Create the long-lived scoped administrator token that will be used for
  # later secret registration and rotation. It receives only the
  # recsys-secrets-admin policy, not root or the default policy. The JSON
  # response is kept only in the private temporary directory.
  admin_token_json="${bootstrap_tmp}/admin-token.json"
  vault_exec "${initial_root_token}" token create \
    -format=json \
    -orphan \
    -no-default-policy \
    -policy=recsys-secrets-admin \
    -display-name=recsys-secrets-admin \
    -ttl=8760h >"${admin_token_json}"
  admin_token="$(jq -er '.auth.client_token' "${admin_token_json}")"

  # Build the final recovery document: remove the initial root token, insert
  # the scoped administrator token, and record that the root token is about to
  # be revoked. This plaintext file exists only in bootstrap_tmp and is removed
  # by the EXIT trap.
  final_bootstrap="${bootstrap_tmp}/vault-bootstrap-final.json"
  jq --arg admin_token "${admin_token}" \
    'del(.root_token) | .recsys_admin_token = $admin_token | .initial_root_token_revoked = true' \
    "${bootstrap_plain}" >"${final_bootstrap}"

  # Encrypt the final recovery document with Google Cloud KMS. Only the
  # ciphertext is allowed to leave the private temporary directory.
  final_ciphertext="${bootstrap_tmp}/vault-init.json.enc"
  encrypt_bootstrap_file "${final_bootstrap}" "${final_ciphertext}"

  # Invalidate the one-time root credential, then atomically replace the early
  # recovery copy with the final KMS-encrypted artifact. The resulting file is
  # .vault-bootstrap/vault-init.json.enc and contains no usable root token.
  echo "Revoking the one-time initial root token..."
  vault_exec "${initial_root_token}" token revoke -self >/dev/null
  mv "${final_ciphertext}" "${artifact_file}"
fi

chmod 600 "${artifact_file}"
echo "Vault bootstrap complete. Encrypted recovery material: ${artifact_file}"
echo "Service secret values were not printed."
