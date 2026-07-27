#!/usr/bin/env bash

gcp_production_field() {
  python3 jenkins/python/configuration.py gcp "$1"
}

gcp_metadata_project_id() {
  curl -fsS -H 'Metadata-Flavor: Google' \
    'http://metadata.google.internal/computeMetadata/v1/project/project-id'
}

gcp_verify_production_target() {
  local image_registry="${IMAGE_PULL_REGISTRY:-${IMAGE_PUSH_REGISTRY:-${IMAGE_REGISTRY:-}}}"
  local expected_project
  local expected_registry
  local expected_context
  local actual_project=""
  local actual_context=""

  expected_project="$(gcp_production_field projectId)"
  expected_registry="$(gcp_production_field imageRegistry)"
  expected_context="$(gcp_production_field context)"
  image_registry="${image_registry%/}"

  [[ "${image_registry}" == "${expected_registry}" ]] || {
    recsys_error "production registry must be ${expected_registry}; got ${image_registry:-<empty>}"
    return 2
  }

  if actual_project="$(gcp_metadata_project_id 2>/dev/null)"; then
    [[ "${actual_project}" == "${expected_project}" ]] || {
      recsys_error "Workload Identity project must be ${expected_project}; got ${actual_project}"
      return 2
    }
  elif command -v gcloud >/dev/null 2>&1; then
    actual_project="$(gcloud config get-value project 2>/dev/null)"
    [[ "${actual_project}" == "${expected_project}" ]] || {
      recsys_error "active gcloud project must be ${expected_project}; got ${actual_project}"
      return 2
    }
  else
    recsys_error "cannot verify GCP project through metadata server or gcloud"
    return 2
  fi

  actual_context="$(kubectl config current-context 2>/dev/null || true)"
  if [[ -n "${actual_context}" ]]; then
    [[ "${actual_context}" == "${expected_context}" ]] || {
      recsys_error "kubectl context must be ${expected_context}; got ${actual_context}"
      return 2
    }
  else
    local target_project target_cluster target_zone
    target_project="$(kubectl get configmap recsys-production-target -n ci -o jsonpath='{.data.projectId}')"
    target_cluster="$(kubectl get configmap recsys-production-target -n ci -o jsonpath='{.data.cluster}')"
    target_zone="$(kubectl get configmap recsys-production-target -n ci -o jsonpath='{.data.zone}')"
    [[ "${target_project}" == "${expected_project}" ]] || return 2
    [[ "${target_cluster}" == "$(gcp_production_field cluster)" ]] || return 2
    [[ "${target_zone}" == "$(gcp_production_field zone)" ]] || return 2
  fi

  kubectl auth can-i get deployments --all-namespaces | grep -Fxq yes
  kubectl auth can-i patch deployments --all-namespaces | grep -Fxq yes
  kubectl wait --for=condition=Ready nodes --all --timeout="${GCP_PREFLIGHT_TIMEOUT:-120s}"
}

gcp_verify_required_crds() {
  local crd
  for crd in \
    inferenceservices.serving.kserve.io \
    scaledobjects.keda.sh \
    rayjobs.ray.io \
    externalsecrets.external-secrets.io \
    servicemonitors.monitoring.coreos.com; do
    kubectl get "crd/${crd}" >/dev/null
  done
}

gcp_verify_workload_identity() {
  local service_account="${JENKINS_KUBERNETES_SERVICE_ACCOUNT:-recsys-jenkins}"
  local namespace="${CI_NAMESPACE:-ci}"
  local annotated_identity
  local token=""
  local metadata_identity=""
  kubectl get serviceaccount "${service_account}" -n "${namespace}" >/dev/null
  annotated_identity="$(
    kubectl get serviceaccount "${service_account}" -n "${namespace}" \
      -o 'jsonpath={.metadata.annotations.iam\.gke\.io/gcp-service-account}'
  )"
  token="$(
    curl -fsS -H 'Metadata-Flavor: Google' \
      'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
  )"
  [[ -n "${token}" ]] || return 2
  metadata_identity="$(
    curl -fsS -H 'Metadata-Flavor: Google' \
      'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email' \
      2>/dev/null || true
  )"
  if [[ -n "${annotated_identity}" && -n "${metadata_identity}" \
    && "${metadata_identity}" != "${annotated_identity}" ]]; then
    recsys_error "Workload Identity token is ${metadata_identity}, expected ${annotated_identity}"
    return 2
  fi
}

gcp_verify_candidate_digests() {
  local component="$1"
  local manifest_path="${IMAGE_MANIFEST_DIR:-.ci-image-manifest}/${component}.env"
  local image_key image_ref digest_key digest_ref
  if [[ "${component}" == "kserve" ]]; then
    return 0
  fi
  [[ -s "${manifest_path}" ]] || {
    recsys_error "candidate image manifest is missing: ${manifest_path}"
    return 2
  }
  while IFS='=' read -r image_key image_ref; do
    [[ "${image_key}" == *_IMAGE ]] || continue
    digest_key="${image_key%_IMAGE}_DIGEST"
    digest_ref="$(awk -F= -v key="${digest_key}" '$1 == key {sub(/^[^=]*=/, "", $0); print; exit}' "${manifest_path}")"
    [[ "${digest_ref}" == "$(gcp_production_field imageRegistry)"/*@sha256:* ]] || {
      recsys_error "${manifest_path} does not contain an immutable production digest for ${image_key}"
      return 2
    }
    docker manifest inspect "${digest_ref}" >/dev/null
  done <"${manifest_path}"
}

gcp_verify_transaction_storage() {
  local root
  local probe
  root="$(
    if declare -F tx_state_root >/dev/null 2>&1; then
      tx_state_root
    else
      printf '%s' "${JENKINS_HOME:?JENKINS_HOME is required}/ci-transactions"
    fi
  )"
  mkdir -p "${root}"
  probe="${root}/.preflight-${BUILD_NUMBER:-manual}-$$"
  : >"${probe}"
  rm -f "${probe}"
}

gcp_helm_targets_for_component() {
  case "$1" in
    materialize|spark_batch|dp1|dp2|dp3|stream_offline|stream_online|drift)
      printf '%s\t%s\n' "${namespace_data:-recsys-dataflow}" recsys-data-platform
      ;;
    training)
      printf '%s\t%s\n' "${namespace_data:-recsys-dataflow}" recsys-data-platform
      printf '%s\t%s\n' "${namespace_mlops:-experiment-tracking}" recsys-mlflow
      ;;
    api|kserve|kserve_model_cd)
      printf '%s\t%s\n' "${namespace_kserve:-kserve-triton-inference}" recsys-serving
      ;;
    analytics)
      printf '%s\t%s\n' "${namespace_data:-recsys-dataflow}" recsys-data-platform
      printf '%s\t%s\n' "${namespace_analytics:-analytics}" recsys-analytics
      ;;
    demo_web)
      printf '%s\t%s\n' recsys-security recsys-security
      printf '%s\t%s\n' "${namespace_demo:-api-serving}" "${DEMO_WEB_RELEASE:-recsys-demo-web}"
      ;;
    mlflow)
      printf '%s\t%s\n' "${namespace_mlops:-experiment-tracking}" recsys-mlflow
      ;;
  esac
}

gcp_verify_helm_history() {
  local namespace release
  while IFS=$'\t' read -r namespace release; do
    [[ -n "${release}" ]] || continue
    if helm status "${release}" -n "${namespace}" >/dev/null 2>&1; then
      helm history "${release}" -n "${namespace}" -o json \
        | python3 -c '
import json, sys
rows = json.load(sys.stdin)
deployed = [row for row in rows if str(row.get("status", "")).lower() == "deployed"]
if not deployed:
    raise SystemExit("release has no deployed revision available for rollback")
'
    fi
  done < <(gcp_helm_targets_for_component "${component:?component is required}")
}

gcp_production_preflight() {
  recsys_require_command curl
  recsys_require_command docker
  recsys_require_command helm
  recsys_require_command kubectl
  python3 jenkins/python/configuration.py validate
  gcp_verify_production_target
  gcp_verify_workload_identity
  gcp_verify_required_crds
  gcp_verify_candidate_digests "${component:?component must be set for production preflight}"
  gcp_verify_transaction_storage
  gcp_verify_helm_history
  recsys_log "validated GCP production target $(gcp_production_field projectId)/$(gcp_production_field cluster)"
}
