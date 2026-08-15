#!/usr/bin/env bash

source jenkins/scripts/lib/config.sh
source jenkins/scripts/lib/registry.sh

gcp_metadata_project_id() {
  curl -fsS -H 'Metadata-Flavor: Google' \
    'http://metadata.google.internal/computeMetadata/v1/project/project-id'
}

gcp_verify_registry_publish_target() {
  local image_registry="${IMAGE_PULL_REGISTRY:-${IMAGE_PUSH_REGISTRY:-${IMAGE_REGISTRY:-}}}"
  local expected_project
  local expected_registry
  local actual_project=""

  load_gcp_production_config
  expected_project="$(gcp_production_field projectId)"
  expected_registry="$(gcp_production_field imageRegistry)"
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
}

gcp_verify_production_target() {
  local expected_project
  local expected_context
  local actual_context=""

  gcp_verify_registry_publish_target
  expected_project="$(gcp_production_field projectId)"
  expected_context="$(gcp_production_field context)"

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

gcp_verify_external_secret_target() {
  local namespace="$1"
  local name="$2"
  shift 2
  kubectl wait --for=condition=Ready "externalsecret/${name}" \
    -n "${namespace}" --timeout="${GCP_PREFLIGHT_TIMEOUT:-120s}"
  if (($# == 0)); then
    kubectl get "secret/${name}" -n "${namespace}" >/dev/null
    return
  fi
  kubectl get "secret/${name}" -n "${namespace}" -o json \
    | python3 -c '
import json
import sys

available = json.load(sys.stdin).get("data", {})
missing = sorted(set(sys.argv[1:]) - set(available))
if missing:
    raise SystemExit(f"Secret is missing required keys: {missing}")
' "$@"
}

gcp_verify_unit_secrets() {
  case "$1" in
    data-config|data-lakehouse|source-store|event-stream|feature-store|kafka-connect|feature-registry|streaming|airflow)
      gcp_verify_external_secret_target recsys-dataflow recsys-data-platform-secret
      ;;
    mlflow)
      gcp_verify_external_secret_target experiment-tracking recsys-mlflow-secrets
      ;;
    serving)
      gcp_verify_external_secret_target kubeflow recsys-mlops-runtime \
        MINIO_ENDPOINT MINIO_ROOT_USER MINIO_ROOT_PASSWORD \
        MLFLOW_TRACKING_URI MLFLOW_EXPERIMENT_NAME MLFLOW_S3_ENDPOINT_URL \
        MODEL_STORE_ENDPOINT MODEL_REGISTRY_POSTGRES_URI MODEL_STORE_BUCKET \
        MODEL_STORE_PREFIX PROMOTION_MANIFEST_KEY AWS_ACCESS_KEY_ID \
        AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION \
        ICEBERG_CATALOG_NAME ICEBERG_WAREHOUSE HUDI_ENABLED HUDI_CATALOG_NAME \
        HUDI_WAREHOUSE HUDI_DATASET_TABLE HUDI_CLEAN_HOURS_RETAINED \
        HUDI_ZK_URL HUDI_ZK_PORT HUDI_ZK_BASE_PATH HUDI_ZK_LOCK_KEY
      gcp_verify_external_secret_target kserve-triton-inference recsys-kserve-minio
      ;;
  esac
}

gcp_verify_candidate_digests() {
  local plan_path="${1:-.ci-release-plan.json}"
  local image_registry
  local image_name digest_ref
  [[ -s "${plan_path}" ]] || {
    recsys_error "release plan is missing: ${plan_path}"
    return 2
  }
  image_registry="$(gcp_production_field imageRegistry)"
  registry_login_gcp "${image_registry}" >/dev/null
  while IFS= read -r image_name; do
    [[ -n "${image_name}" ]] || continue
    digest_ref="$(image_manifest_lookup "${image_name}")"
    [[ "${digest_ref}" == "${image_registry}"/*@sha256:* ]] || {
      recsys_error "release manifest does not contain an immutable digest for ${image_name}"
      return 2
    }
    docker manifest inspect "${digest_ref}" >/dev/null
  done < <(
    python3 jenkins/python/release_plan.py plan-images --plan "${plan_path}"
  )
}

gcp_verify_helm_history() {
  local kind="$1"
  local namespace="$2"
  local release="$3"
  [[ "${kind}" == "helm" ]] || return 0
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
}

verify_gcp_release_target() {
  local plan_path="${1:-.ci-release-plan.json}"
  load_gcp_production_config
  recsys_require_command curl
  recsys_require_command docker
  recsys_require_command helm
  recsys_require_command kubectl
  gcp_verify_production_target
  gcp_verify_workload_identity
  gcp_verify_required_crds
  gcp_verify_candidate_digests "${plan_path}"
  recsys_log "validated GCP production target $(gcp_production_field projectId)/$(gcp_production_field cluster)"
}

verify_gcp_deploy_unit() {
  local unit_name="$1"
  local unit_kind="$2"
  local unit_namespace="$3"
  local unit_release="$4"
  gcp_verify_unit_secrets "${unit_name}"
  gcp_verify_helm_history "${unit_kind}" "${unit_namespace}" "${unit_release}"
  recsys_log "validated deploy-unit prerequisites for ${unit_name}"
}
