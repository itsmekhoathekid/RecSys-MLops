#!/usr/bin/env bash

registry_host_from_repository() {
  printf '%s' "${1%%/*}"
}

registry_validate_gcp_repository() {
  local repository="${1%/}"
  local expected="$2"
  if [[ "${repository}" != "${expected}" ]]; then
    recsys_error "registry mismatch: expected ${expected}, got ${repository}"
    return 2
  fi
}

registry_gcp_access_token() {
  local token=""
  if token="$(
    curl -fsS -H 'Metadata-Flavor: Google' \
      'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
  )"; then
    printf '%s' "${token}"
    return 0
  fi
  if command -v gcloud >/dev/null 2>&1; then
    gcloud auth print-access-token
    return
  fi
  recsys_error "unable to obtain a GCP access token through Workload Identity or gcloud"
  return 1
}

registry_login_gcp() {
  local repository="$1"
  local registry_host
  local token
  registry_host="$(registry_host_from_repository "${repository}")"
  token="$(registry_gcp_access_token)"
  printf '%s' "${token}" \
    | docker login "https://${registry_host}" --username oauth2accesstoken --password-stdin
}

registry_verify_gcp_upload_permission() {
  local token
  local project
  local region
  local response
  project="$(gcp_production_field projectId)"
  region="$(gcp_production_field region)"
  token="$(registry_gcp_access_token)"
  response="$(
    curl -fsS \
      -H "Authorization: Bearer ${token}" \
      -H 'Content-Type: application/json' \
      --data '{"permissions":["artifactregistry.repositories.uploadArtifacts"]}' \
      "https://artifactregistry.googleapis.com/v1/projects/${project}/locations/${region}/repositories/recsys:testIamPermissions"
  )"
  python3 -c '
import json
import sys
payload = json.load(sys.stdin)
permission = "artifactregistry.repositories.uploadArtifacts"
if permission not in payload.get("permissions", []):
    raise SystemExit(f"Workload Identity principal lacks {permission}")
' <<<"${response}"
}
