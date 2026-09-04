#!/usr/bin/env bash

source jenkins/scripts/lib/config.sh

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

registry_resolve_digest_reference() {
  local reference="$1"
  local expected_repository="${2%/}"
  local registry_host
  local manifest_reference
  local manifest_repository
  local manifest_tag
  local response_headers
  local digest
  local token

  if [[ "${reference}" == *@sha256:* ]]; then
    [[ "${reference}" == "${expected_repository}/"* ]] || {
      recsys_error "image reference is outside ${expected_repository}: ${reference}"
      return 2
    }
    printf '%s' "${reference}"
    return 0
  fi

  [[ "${reference}" == "${expected_repository}/"* ]] || {
    recsys_error "image reference is outside ${expected_repository}: ${reference}"
    return 2
  }

  registry_host="$(registry_host_from_repository "${reference}")"
  manifest_reference="${reference#${registry_host}/}"
  [[ "${manifest_reference}" == *:* ]] || {
    recsys_error "image reference has no tag or digest: ${reference}"
    return 2
  }
  manifest_repository="${manifest_reference%:*}"
  manifest_tag="${manifest_reference##*:}"
  [[ -n "${manifest_repository}" && -n "${manifest_tag}" ]] || {
    recsys_error "invalid tagged image reference: ${reference}"
    return 2
  }

  token="$(registry_gcp_access_token)"
  response_headers="$(
    curl -fsSI \
      -H "Authorization: Bearer ${token}" \
      -H 'Accept: application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json' \
      "https://${registry_host}/v2/${manifest_repository}/manifests/${manifest_tag}"
  )"
  digest="$(
    awk '
tolower($1) == "docker-content-digest:" {
  gsub(/\r/, "", $2)
  print $2
}
' <<<"${response_headers}" | tail -n 1
  )"
  [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    recsys_error "registry did not return an immutable digest for ${reference}"
    return 2
  }

  printf '%s/%s@%s' "${registry_host}" "${manifest_repository}" "${digest}"
}

registry_resolve_latest_digest_reference() {
  local image_name="$1"
  local expected_repository="${2%/}"
  local project
  local region
  local repository_name
  local token
  local page_token=""
  local response
  local result
  local digest_reference
  local query_args=()

  project="$(gcp_production_field projectId)"
  region="$(gcp_production_field region)"
  repository_name="${expected_repository##*/}"
  token="$(registry_gcp_access_token)"

  for _ in $(seq 1 10); do
    query_args=(
      --data-urlencode 'pageSize=1000'
      --data-urlencode 'orderBy=UPDATE_TIME desc'
    )
    [[ -z "${page_token}" ]] \
      || query_args+=(--data-urlencode "pageToken=${page_token}")
    response="$(
      curl -fsS -G \
        -H "Authorization: Bearer ${token}" \
        "${query_args[@]}" \
        "https://artifactregistry.googleapis.com/v1/projects/${project}/locations/${region}/repositories/${repository_name}/dockerImages"
    )"
    result="$(
      python3 -c '
import json
import sys

repository, image = sys.argv[1:]
payload = json.load(sys.stdin)
prefix = f"{repository}/{image}@sha256:"
match = next(
    (item.get("uri", "") for item in payload.get("dockerImages", [])
     if item.get("uri", "").startswith(prefix)),
    "",
)
print(match)
print(payload.get("nextPageToken", ""))
' "${expected_repository}" "${image_name}" <<<"${response}"
    )"
    digest_reference="$(sed -n '1p' <<<"${result}")"
    page_token="$(sed -n '2p' <<<"${result}")"
    if [[ "${digest_reference}" =~ @sha256:[0-9a-f]{64}$ ]]; then
      recsys_log REGISTRY "reusing latest immutable ${image_name} digest" >&2
      printf '%s' "${digest_reference}"
      return 0
    fi
    [[ -n "${page_token}" ]] || break
  done

  recsys_error "Artifact Registry has no immutable image for ${expected_repository}/${image_name}"
  return 2
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
