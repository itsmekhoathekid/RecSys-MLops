#!/usr/bin/env bash

deploy_drift() {
  deploy_data_platform --set "images.driftRetrain=$(image recsys-drift-retrain)"
  verify_data_platform_config_image "DRIFT_RETRAIN_IMAGE" "$(image recsys-drift-retrain)"
  if [[ -d infra/knative/recsys-drift ]]; then
    local state_path="${TX_DIR}/drift-knative-resources.txt"
    recsys_error "raw Knative drift manifests require typed compensation before production use: ${state_path}"
    return 2
  fi
  recsys_log "deployed drift-capable data platform image"
}
