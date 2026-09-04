#!/usr/bin/env bash
set -uo pipefail

MODE="${1:-all}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="${ROOT_DIR}/infra/terraform/gcp"
GATEWAY_CHECK_USER="${GCP_CHECK_BASIC_AUTH_USER:-${GATEWAY_AUTH_USER:-${GATEWAY_USER:-}}}"
GATEWAY_CHECK_PASSWORD="${GCP_CHECK_BASIC_AUTH_PASSWORD:-${GATEWAY_AUTH_PASSWORD:-${GATEWAY_PASSWORD:-}}}"
PROJECT_ID="${GCP_PROJECT_ID:-recsys-mlops-506406}"
REGION="${GCP_REGION:-asia-southeast1}"
ZONE="${GKE_ZONE:-asia-southeast1-b}"
CLUSTER="${GKE_CLUSTER:-recsys-mlops-gke}"
STATE_BUCKET="${TF_STATE_BUCKET:-recsys-mlops-506406-tfstate}"
OUTPUT_JSON="${GCP_FULL_CHECK_JSON:-${ROOT_DIR}/reports/gcp/full-stack-check.json}"
RESULTS_FILE="$(mktemp)"
trap 'rm -f "${RESULTS_FILE}"' EXIT

record() {
  local id="$1" status="$2" description="$3" detail="${4:-}"
  detail="${detail//$'\t'/ }"
  detail="${detail//$'\n'/ }"
  printf '%s\t%s\t%s\t%s\n' "${id}" "${status}" "${description}" "${detail}" >>"${RESULTS_FILE}"
  printf '%-6s %-34s %s\n' "${status}" "${id}" "${description}"
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

check_https_unauthenticated() {
  local id="$1" description="$2" url="$3" status
  if status="$(curl -sSIL --max-time 15 -o /dev/null -w '%{http_code}' "${url}")" && [[ "${status}" == 401 ]]; then
    record "${id}" PASS "${description}" "HTTP ${status}"
  else
    record "${id}" FAIL "${description}" "HTTP ${status:-unreachable}"
  fi
}

check_https_authenticated() {
  local id="$1" description="$2" url="$3" host netrc_file
  if [[ -z "${GATEWAY_CHECK_USER}" || -z "${GATEWAY_CHECK_PASSWORD}" ]]; then
    record "${id}" FAIL "${description}" "gateway basic-auth credentials are not configured"
    return
  fi
  host="${url#https://}"
  host="${host%%/*}"
  netrc_file="$(mktemp)"
  chmod 600 "${netrc_file}"
  printf 'machine %s login %s password %s\n' \
    "${host}" "${GATEWAY_CHECK_USER}" "${GATEWAY_CHECK_PASSWORD}" >"${netrc_file}"
  check_cmd "${id}" "${description}" \
    curl -fsSL -o /dev/null --netrc-file "${netrc_file}" --max-time 15 "${url}"
  rm -f "${netrc_file}"
}

check_https_authenticated_status() {
  local id="$1" description="$2" url="$3" expected_status="$4" host netrc_file status
  if [[ -z "${GATEWAY_CHECK_USER}" || -z "${GATEWAY_CHECK_PASSWORD}" ]]; then
    record "${id}" FAIL "${description}" "gateway basic-auth credentials are not configured"
    return
  fi
  host="${url#https://}"
  host="${host%%/*}"
  netrc_file="$(mktemp)"
  chmod 600 "${netrc_file}"
  printf 'machine %s login %s password %s\n' \
    "${host}" "${GATEWAY_CHECK_USER}" "${GATEWAY_CHECK_PASSWORD}" >"${netrc_file}"
  status="$(curl -sS --netrc-file "${netrc_file}" --max-time 15 -o /dev/null -w '%{http_code}' "${url}" || true)"
  rm -f "${netrc_file}"
  if [[ "${status}" == "${expected_status}" ]]; then
    record "${id}" PASS "${description}" "HTTP ${status}"
  else
    record "${id}" FAIL "${description}" "expected HTTP ${expected_status}, got ${status:-unreachable}"
  fi
}

static_checks() {
  check_cmd repo-config "Jenkins/image production configuration is valid" \
    python3 "${ROOT_DIR}/jenkins/python/configuration.py" validate
  check_cmd terraform-fmt "Terraform formatting is clean" \
    terraform -chdir="${TF_DIR}" fmt -check -recursive
  if [[ -d "${TF_DIR}/.terraform" ]]; then
    check_cmd terraform-validate "Terraform provider configuration is valid" \
      bash "${ROOT_DIR}/ops/gcp/terraform_gcp.sh" -chdir="${TF_DIR}" validate
  else
    record terraform-validate SKIP "Terraform is not initialized yet" "run terraform init"
  fi
  check_cmd helm-serving-cpu "KServe/Triton CPU fallback renders" \
    helm template recsys-serving "${ROOT_DIR}/infra/helm/recsys-serving" -f "${ROOT_DIR}/infra/helm/recsys-serving/values-gcp-cpu.yaml"
  check_cmd helm-serving-gpu "KServe/Triton GPU profile renders" \
    helm template recsys-serving "${ROOT_DIR}/infra/helm/recsys-serving" -f "${ROOT_DIR}/infra/helm/recsys-serving/values-gcp-gpu.yaml"
  check_cmd helm-ray-cpu "Ray CPU fallback renders" \
    helm template recsys-ray "${ROOT_DIR}/infra/helm/ray-cluster" -f "${ROOT_DIR}/infra/helm/ray-cluster/values-gcp-cpu.yaml"
  check_cmd helm-ray-gpu "Ray GPU profile renders" \
    helm template recsys-ray "${ROOT_DIR}/infra/helm/ray-cluster" -f "${ROOT_DIR}/infra/helm/ray-cluster/values-gcp-gpu.yaml"
  check_cmd helm-llm-dedicated "Dedicated LLM rollback profile renders" \
    helm template recsys-llm-serving "${ROOT_DIR}/infra/helm/recsys-llm-serving" -f "${ROOT_DIR}/infra/helm/recsys-llm-serving/values-gcp.yaml"
  check_cmd helm-llm-shared "Shared LLM cost profile renders" \
    helm template recsys-llm-serving "${ROOT_DIR}/infra/helm/recsys-llm-serving" -f "${ROOT_DIR}/infra/helm/recsys-llm-serving/values-cpu-shared.yaml"
  check_cmd helm-gateway "Production gateway routes render" \
    helm template recsys-gateway "${ROOT_DIR}/infra/helm/recsys-gateway" \
      --namespace api-serving --kube-version 1.35.0 \
      -f "${ROOT_DIR}/infra/helm/recsys-gateway/values-gcp.yaml"
  check_cmd helm-security "Security secret replication renders" \
    helm template recsys-security "${ROOT_DIR}/infra/helm/recsys-security" \
      --namespace recsys-security --kube-version 1.35.0
  check_cmd topology-config "Production config selects the exact three-node CPU-only topology" \
    bash -c 'grep -Eq '\''^cpu_machine_type *= *"e2-standard-8"$'\'' "$1" && grep -Eq '\''^cpu_min_nodes *= *2$'\'' "$1" && grep -Eq '\''^cpu_max_nodes *= *2$'\'' "$1" && grep -Eq '\''^ml_machine_type *= *"e2-standard-4"$'\'' "$1" && grep -Eq '\''^ml_min_nodes *= *1$'\'' "$1" && grep -Eq '\''^ml_max_nodes *= *1$'\'' "$1" && grep -Eq '\''^enable_gpu_pool *= *false$'\'' "$1" && grep -Eq '\''^llm_node_pool_mode *= *"cpu-services-shared"$'\'' "$1"' _ "${TF_DIR}/terraform.tfvars"
  check_cmd inventory-images "Image catalog contains exactly 19 images" \
    bash -c '[[ "$(jq ".images | length" "$1")" == 19 ]]' _ "${ROOT_DIR}/images/catalog.json"
  check_cmd inventory-components "CI configuration contains exactly 22 product components" \
    bash -c '[[ "$(jq ".components | length" "$1")" == 22 ]]' _ "${ROOT_DIR}/jenkins/config/components.json"
  check_cmd inventory-units "Release graph contains exactly 33 deploy units" \
    bash -c '[[ "$(jq ".units | length" "$1")" == 33 ]]' _ "${ROOT_DIR}/jenkins/config/deploy-units.json"
}

preflight_checks() {
  check_cmd gcloud-account "An active gcloud account exists" \
    gcloud auth list --filter=status:ACTIVE --format='value(account)'
  check_cmd gcloud-project "Active gcloud project is the guarded production target" \
    bash -c '[[ "$(gcloud config get-value project 2>/dev/null)" == "$1" ]]' _ "${PROJECT_ID}"
  check_cmd project-active "GCP project is ACTIVE" \
    bash -c '[[ "$(gcloud projects describe "$1" --format="value(lifecycleState)")" == ACTIVE ]]' _ "${PROJECT_ID}"
  check_cmd billing-enabled "Billing is enabled" \
    bash -c '[[ "$(gcloud beta billing projects describe "$1" --format="value(billingEnabled)")" == True ]]' _ "${PROJECT_ID}"

  local api
  for api in \
    artifactregistry.googleapis.com cloudkms.googleapis.com \
    cloudresourcemanager.googleapis.com compute.googleapis.com \
    container.googleapis.com iam.googleapis.com logging.googleapis.com \
    monitoring.googleapis.com storage.googleapis.com serviceusage.googleapis.com; do
    check_cmd "api-${api%%.*}" "API enabled: ${api}" \
      bash -c 'gcloud services list --enabled --project="$1" --filter="config.name=$2" --format="value(config.name)" | grep -Fx "$2"' _ "${PROJECT_ID}" "${api}"
  done

  check_cmd terraform-state "Versioned Terraform state bucket exists" \
    bash -c 'gcloud storage buckets describe "gs://$1" --project="$2" --format="value(versioning_enabled)" | grep -Fx True' _ "${STATE_BUCKET}" "${PROJECT_ID}"
  check_cmd artifact-registry "Production Artifact Registry exists" \
    gcloud artifacts repositories describe recsys --location "${REGION}" --project "${PROJECT_ID}"
  check_cmd core-cpu-quota "Regional CPU quota limit covers the selected 20-vCPU topology" \
    bash -c 'gcloud compute regions describe "$1" --project="$2" --format=json | jq -e '\''[.quotas[] | select(.metric == "CPUS")][0].limit >= 20'\''' _ "${REGION}" "${PROJECT_ID}"
  record gpu-quota SKIP "GPU quota is not required by the selected CPU-only cost profile" "enable_gpu_pool=false"
}

live_checks() {
  local expected_context="gke_${PROJECT_ID}_${ZONE}_${CLUSTER}"
  check_cmd kube-context "kubectl context matches the production cluster" \
    bash -c '[[ "$(kubectl config current-context)" == "$1" ]]' _ "${expected_context}"
  check_cmd cluster-ready "GKE control plane is RUNNING" \
    bash -c '[[ "$(gcloud container clusters describe "$1" --zone "$2" --project "$3" --format="value(status)")" == RUNNING ]]' _ "${CLUSTER}" "${ZONE}" "${PROJECT_ID}"
  check_cmd nodes-ready "Every Kubernetes node is Ready" \
    bash -c 'kubectl get nodes -o json | jq -e '\''(.items | length) > 0 and all(.items[]; any(.status.conditions[]; .type == "Ready" and .status == "True"))'\'''
  check_cmd node-pools-exact "Only the CPU and ML node pools exist" \
    bash -c '[[ "$(gcloud container node-pools list --cluster "$1" --zone "$2" --project "$3" --format="value(name)" | sort)" == $'\''recsys-mlops-cpu\nrecsys-mlops-ml-system'\'' ]]' _ "${CLUSTER}" "${ZONE}" "${PROJECT_ID}"
  check_cmd node-topology-exact "GKE has 2 x e2-standard-8 CPU nodes and 1 x e2-standard-4 ML node" \
    bash -c 'kubectl get nodes -o json | jq -e '\''(.items | length) == 3 and ([.items[] | select(.metadata.labels["cloud.google.com/gke-nodepool"] == "recsys-mlops-cpu" and .metadata.labels["node.kubernetes.io/instance-type"] == "e2-standard-8")] | length) == 2 and ([.items[] | select(.metadata.labels["cloud.google.com/gke-nodepool"] == "recsys-mlops-ml-system" and .metadata.labels["node.kubernetes.io/instance-type"] == "e2-standard-4")] | length) == 1'\'''
  check_cmd qwen-spread "Two Ready Qwen replicas run on different shared CPU nodes" \
    bash -c 'kubectl get pods -n llm-inference -l app.kubernetes.io/name=qwen35-gguf -o json | jq -e '\''(.items | length) == 2 and all(.items[]; .spec.nodeSelector["cloud.google.com/gke-nodepool"] == "recsys-mlops-cpu" and any(.status.conditions[]?; .type == "Ready" and .status == "True")) and ([.items[].spec.nodeName] | unique | length) == 2'\'''
  check_cmd workloads-ready "Every Deployment, StatefulSet and DaemonSet satisfies desired readiness" \
    bash -c 'kubectl get deployment,statefulset,daemonset -A -o json | jq -e '\''all(.items[]; if .kind == "DaemonSet" then (.status.numberReady // 0) >= (.status.desiredNumberScheduled // 0) elif .kind == "StatefulSet" then (.status.readyReplicas // 0) >= (.spec.replicas // 0) else (.status.readyReplicas // 0) >= (.spec.replicas // 0) end)'\'''
  check_cmd pvcs-bound "All persistent volume claims are Bound" \
    bash -c 'kubectl get pvc -A -o json | jq -e '\''(.items | length) > 0 and all(.items[]; .status.phase == "Bound")'\'''
  check_cmd external-secrets "All ExternalSecrets report Ready" \
    bash -c 'kubectl get externalsecret -A -o json | jq -e '\''(.items | length) > 0 and all(.items[]; any(.status.conditions[]?; .type == "Ready" and .status == "True"))'\'''
  check_cmd public-ui-auth-secrets "Gateway Basic Auth is replicated to both UI namespaces" \
    bash -c 'kubectl get secret recsys-gateway-basic-auth -n kagent >/dev/null && kubectl get secret recsys-gateway-basic-auth -n agentregistry >/dev/null'

  local release_specs=(
    "cert-manager:cert-manager" "keda:keda" "keda-add-ons-http:keda"
    "external-secrets:external-secrets" "istio-base:istio-system" "istiod:istio-system"
    "ingress-nginx:ingress-nginx" "kuberay-operator:kubeflow"
    "recsys-observability:observability" "recsys-mlflow:experiment-tracking"
    "recsys-runtime:kubeflow" "recsys-data-config:recsys-dataflow"
    "recsys-data-lakehouse:recsys-dataflow" "recsys-source-store:recsys-dataflow"
    "recsys-event-stream:recsys-dataflow" "recsys-feature-store:recsys-dataflow"
    "recsys-kafka-connect:recsys-dataflow" "recsys-streaming:recsys-dataflow"
    "recsys-airflow:recsys-dataflow" "recsys-online-feature-api:api-serving"
    "recsys-inference-api:api-serving" "recsys-serving:kserve-triton-inference"
    "recsys-security:recsys-security" "recsys-gateway:api-serving"
    "prerequisites:datahub" "datahub:datahub" "vault:vault"
    "substrate-crds:ate-system" "substrate:ate-system" "kagent-crds:kagent"
    "kagent:kagent" "agentgateway-crds:agentgateway-system" "agentgateway:agentgateway-system"
    "recsys-llm-serving:llm-inference" "llm-d-optimized-baseline:llm-inference"
    "agentregistry-postgres:agentregistry" "agentregistry:agentregistry"
    "recsys-ci:ci" "recsys-rag-api:api-serving" "recsys-feature-rag-mcp:kagent"
    "recsys-kagent-agent:kagent" "recsys-recommendation-mcp:kagent"
    "recsys-recommendation-agent:kagent" "recsys-analytics:analytics" "recsys-demo-web:api-serving"
  )
  local spec release namespace
  for spec in "${release_specs[@]}"; do
    release="${spec%%:*}"
    namespace="${spec#*:}"
    check_cmd "helm-${release}" "Helm release deployed: ${namespace}/${release}" \
      bash -c '[[ "$(helm status "$1" -n "$2" -o json | jq -r .info.status)" == deployed ]]' _ "${release}" "${namespace}"
  done

  check_cmd kfp-api "Kubeflow Pipelines API deployment is available" kubectl rollout status deployment/ml-pipeline -n kubeflow --timeout=5s
  check_cmd kserve-stable "Stable KServe InferenceService exists" kubectl get inferenceservice recsys-bst-triton -n kserve-triton-inference
  check_cmd mlflow "MLflow deployment is available" kubectl rollout status deployment/mlflow -n experiment-tracking --timeout=5s
  check_cmd airflow "Airflow webserver is available" kubectl rollout status deployment/airflow-webserver -n recsys-dataflow --timeout=5s
  check_cmd datahub "DataHub GMS is available" kubectl rollout status deployment/datahub-datahub-gms -n datahub --timeout=5s
  check_cmd vault "Vault is initialized and unsealed" \
    bash -c 'kubectl exec -n vault vault-0 -- env VAULT_ADDR=http://127.0.0.1:8200 vault status -format=json | jq -e '\''.initialized and (.sealed | not)'\'''
  check_cmd certificate-base "Base-domain TLS certificate is Ready" \
    bash -c 'kubectl get certificate -A -o json | jq -e '\''any(.items[]; any(.status.conditions[]?; .type == "Ready" and .status == "True"))'\'''

  local url
  for url in \
    "https://recsys-mlops.site/" \
    "https://api.recsys-mlops.site/docs" \
    "https://metrics.recsys-mlops.site/" \
    "https://logs.recsys-mlops.site/ready" \
    "https://traces.recsys-mlops.site/ready" \
    "https://agents.recsys-mlops.site/" \
    "https://registry.recsys-mlops.site/" \
    "https://rag.recsys-mlops.site/docs" \
    "https://rag.recsys-mlops.site/openapi.json"; do
    url_hash="$(printf '%s' "${url}" | sha1sum | cut -c1-8)"
    check_https_unauthenticated "https-unauth-${url_hash}" \
      "Public HTTPS endpoint rejects missing basic auth: ${url}" "${url}"
    check_https_authenticated "https-${url_hash}" \
      "Public HTTPS endpoint responds with valid basic auth: ${url}" "${url}"
  done

  local private_path path_hash
  for private_path in metrics healthz ready version; do
    path_hash="$(printf '%s' "${private_path}" | sha1sum | cut -c1-8)"
    check_https_authenticated_status "rag-private-${path_hash}" \
      "RAG operational endpoint remains private: /${private_path}" \
      "https://rag.recsys-mlops.site/${private_path}" "404"
  done
}

case "${MODE}" in
  static) static_checks ;;
  preflight) static_checks; preflight_checks ;;
  live) live_checks ;;
  all) static_checks; preflight_checks; live_checks ;;
  *) echo "Usage: $0 [static|preflight|live|all]" >&2; exit 2 ;;
esac

mkdir -p "$(dirname "${OUTPUT_JSON}")"
jq -Rn '[inputs | split("\t") | {id: .[0], status: .[1], description: .[2], detail: .[3]}]' \
  <"${RESULTS_FILE}" >"${OUTPUT_JSON}"
failures="$(jq '[.[] | select(.status == "FAIL")] | length' "${OUTPUT_JSON}")"
passes="$(jq '[.[] | select(.status == "PASS")] | length' "${OUTPUT_JSON}")"
skips="$(jq '[.[] | select(.status == "SKIP")] | length' "${OUTPUT_JSON}")"
printf '\nSummary: PASS=%s FAIL=%s SKIP=%s\nJSON: %s\n' "${passes}" "${failures}" "${skips}" "${OUTPUT_JSON}"
[[ "${failures}" == 0 ]]
