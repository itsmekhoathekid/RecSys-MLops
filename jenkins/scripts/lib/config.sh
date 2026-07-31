#!/usr/bin/env bash

load_gcp_production_config() {
  if [[ "${GCP_PRODUCTION_CONFIG_LOADED:-0}" == "1" ]]; then
    return 0
  fi
  IFS=$'\t' read -r \
    GCP_CLUSTER GCP_CONTEXT GCP_IMAGE_REGISTRY GCP_PROJECT_ID GCP_REGION GCP_ZONE < <(
      python3 jenkins/python/configuration.py gcp-tsv
    )
  GCP_PRODUCTION_CONFIG_LOADED=1
  export GCP_CLUSTER GCP_CONTEXT GCP_IMAGE_REGISTRY GCP_PROJECT_ID GCP_REGION GCP_ZONE
  export GCP_PRODUCTION_CONFIG_LOADED
}

gcp_production_field() {
  load_gcp_production_config
  case "$1" in
    cluster) printf '%s' "${GCP_CLUSTER}" ;;
    context) printf '%s' "${GCP_CONTEXT}" ;;
    imageRegistry) printf '%s' "${GCP_IMAGE_REGISTRY}" ;;
    projectId) printf '%s' "${GCP_PROJECT_ID}" ;;
    region) printf '%s' "${GCP_REGION}" ;;
    zone) printf '%s' "${GCP_ZONE}" ;;
    *)
      printf 'unknown GCP production field: %s\n' "$1" >&2
      return 2
      ;;
  esac
}
