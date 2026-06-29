#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

VAULT_NAMESPACE="${RECSYS_VAULT_NAMESPACE:-vault}"
VAULT_RELEASE="${RECSYS_VAULT_RELEASE:-vault}"
VAULT_TOKEN="${RECSYS_VAULT_TOKEN:-recsys-root}"
VAULT_KV_MOUNT="${RECSYS_VAULT_KV_MOUNT:-recsys}"
ESO_NAMESPACE="${RECSYS_ESO_NAMESPACE:-external-secrets}"
ISTIO_NAMESPACE="${RECSYS_ISTIO_NAMESPACE:-istio-system}"
WAIT_TIMEOUT="${RECSYS_SECURITY_WAIT_TIMEOUT:-300s}"

TARGET_NAMESPACES=(
  kubeflow
  experiment-tracking
  recsys-dataflow
  kserve
  kserve-triton-inference
  api-serving
  observability
  ingress-nginx
  keda
)

section() {
  printf "\n== %s ==\n" "$1"
}

ensure_namespace() {
  local namespace="$1"
  kubectl create namespace "${namespace}" --dry-run=client -o yaml | kubectl apply -f -
}

install_external_secrets() {
  section "Install External Secrets Operator"
  helm repo add external-secrets https://charts.external-secrets.io >/dev/null
  helm repo update external-secrets >/dev/null
  helm upgrade --install external-secrets external-secrets/external-secrets \
    --namespace "${ESO_NAMESPACE}" \
    --create-namespace \
    --set installCRDs=true \
    --wait \
    --timeout 5m
  kubectl rollout status deploy/external-secrets -n "${ESO_NAMESPACE}" --timeout="${WAIT_TIMEOUT}"
  kubectl rollout status deploy/external-secrets-webhook -n "${ESO_NAMESPACE}" --timeout="${WAIT_TIMEOUT}"
}

install_vault() {
  section "Install Vault Dev Server"
  helm repo add hashicorp https://helm.releases.hashicorp.com >/dev/null
  helm repo update hashicorp >/dev/null
  helm upgrade --install "${VAULT_RELEASE}" hashicorp/vault \
    --namespace "${VAULT_NAMESPACE}" \
    --create-namespace \
    --set server.dev.enabled=true \
    --set server.dev.devRootToken="${VAULT_TOKEN}" \
    --set injector.enabled=false \
    --wait \
    --timeout 5m
  kubectl wait --for=condition=Ready pod/"${VAULT_RELEASE}-0" -n "${VAULT_NAMESPACE}" --timeout="${WAIT_TIMEOUT}"
}

vault_exec() {
  kubectl exec -n "${VAULT_NAMESPACE}" statefulset/"${VAULT_RELEASE}" -- sh -c "VAULT_TOKEN='${VAULT_TOKEN}' $*"
}

configure_vault_kubernetes_auth() {
  section "Configure Vault Kubernetes Auth"
  vault_exec "vault secrets enable -path='${VAULT_KV_MOUNT}' kv-v2 >/dev/null 2>&1 || true"
  vault_exec "vault auth enable kubernetes >/dev/null 2>&1 || true"
  vault_exec "vault write auth/kubernetes/config kubernetes_host=https://kubernetes.default.svc.cluster.local token_reviewer_jwt=\"\$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)\" kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
  kubectl exec -i -n "${VAULT_NAMESPACE}" statefulset/"${VAULT_RELEASE}" -- sh -c "cat >/tmp/recsys-read.hcl && VAULT_TOKEN='${VAULT_TOKEN}' vault policy write recsys-read /tmp/recsys-read.hcl" <<POLICY
path "${VAULT_KV_MOUNT}/data/*" {
  capabilities = ["read"]
}
POLICY
  vault_exec "vault write auth/kubernetes/role/recsys-external-secrets bound_service_account_names=external-secrets bound_service_account_namespaces='${ESO_NAMESPACE}' policies=recsys-read ttl=24h"
}

seed_vault_secrets() {
  section "Seed Vault Secrets For Local E2E"
  vault_exec "vault kv put '${VAULT_KV_MOUNT}/data-platform' DATA_PLATFORM_MINIO_ROOT_USER=minio DATA_PLATFORM_MINIO_ROOT_PASSWORD=minio123 MINIO_ROOT_USER=minio MINIO_ROOT_PASSWORD=minio123 AWS_ACCESS_KEY_ID=minio AWS_SECRET_ACCESS_KEY=minio123 POSTGRES_USER=recsys POSTGRES_PASSWORD=recsys AIRFLOW_POSTGRES_USER=airflow AIRFLOW_POSTGRES_PASSWORD=airflow"
  vault_exec "vault kv put '${VAULT_KV_MOUNT}/mlflow' MINIO_ROOT_USER=minio MINIO_ROOT_PASSWORD=minio123 POSTGRES_DB=mlflow POSTGRES_USER=mlflow POSTGRES_PASSWORD=mlflow123"
  vault_exec "vault kv put '${VAULT_KV_MOUNT}/runtime' MINIO_ENDPOINT=http://data-platform-minio.recsys-dataflow.svc.cluster.local:9000 MINIO_ROOT_USER=minio MINIO_ROOT_PASSWORD=minio123 AWS_ACCESS_KEY_ID=minio AWS_SECRET_ACCESS_KEY=minio123 AWS_DEFAULT_REGION=us-east-1 MLFLOW_S3_ENDPOINT_URL=http://minio.experiment-tracking.svc.cluster.local:9000 MODEL_STORE_ENDPOINT=http://minio.experiment-tracking.svc.cluster.local:9000 MLFLOW_TRACKING_URI=http://mlflow.experiment-tracking.svc.cluster.local:5000 MLFLOW_EXPERIMENT_NAME=recsys-bst-ranking MODEL_REGISTRY_POSTGRES_URI=postgresql://mlflow:mlflow123@postgres.experiment-tracking.svc.cluster.local:5432/mlflow MODEL_STORE_BUCKET=recsys-model-store MODEL_STORE_PREFIX=triton/bst PROMOTION_MANIFEST_KEY=promotions/bst/latest.json ICEBERG_ENABLED=true ICEBERG_CATALOG_NAME=recsys_features ICEBERG_WAREHOUSE=s3a://recsys-offline-feature-store/warehouse HUDI_ENABLED=true HUDI_CATALOG_NAME=recsys_features HUDI_WAREHOUSE=s3a://recsys-offline-feature-store/warehouse"
  vault_exec "vault kv put '${VAULT_KV_MOUNT}/kserve-minio' AWS_ACCESS_KEY_ID=minio AWS_SECRET_ACCESS_KEY=minio123 AWS_DEFAULT_REGION=us-east-1 AWS_ENDPOINT_URL=http://minio.experiment-tracking.svc.cluster.local:9000 S3_ENDPOINT=minio.experiment-tracking.svc.cluster.local:9000 S3_USE_HTTPS=0"
  vault_exec "vault kv put '${VAULT_KV_MOUNT}/gateway' auth=recsys:\$apr1\$local\$wRF6uR8BMLhFWxwG84foS/"
}

install_istio() {
  section "Install Istio"
  helm repo add istio https://istio-release.storage.googleapis.com/charts >/dev/null
  helm repo update istio >/dev/null
  helm upgrade --install istio-base istio/base -n "${ISTIO_NAMESPACE}" --create-namespace --wait --timeout 5m
  helm upgrade --install istiod istio/istiod -n "${ISTIO_NAMESPACE}" \
    --set global.configValidation=false \
    --wait \
    --timeout 5m
  kubectl rollout status deploy/istiod -n "${ISTIO_NAMESPACE}" --timeout="${WAIT_TIMEOUT}"
  kubectl delete validatingwebhookconfiguration "istio-validator-${ISTIO_NAMESPACE}" istiod-default-validator --ignore-not-found
}

stabilize_external_secrets() {
  section "Stabilize External Secrets Webhook"
  kubectl rollout restart deploy/external-secrets deploy/external-secrets-cert-controller deploy/external-secrets-webhook -n "${ESO_NAMESPACE}"
  kubectl rollout status deploy/external-secrets -n "${ESO_NAMESPACE}" --timeout="${WAIT_TIMEOUT}"
  kubectl rollout status deploy/external-secrets-cert-controller -n "${ESO_NAMESPACE}" --timeout="${WAIT_TIMEOUT}"
  kubectl rollout status deploy/external-secrets-webhook -n "${ESO_NAMESPACE}" --timeout="${WAIT_TIMEOUT}"
}

prepare_namespaces_for_mesh() {
  section "Prepare Namespaces For Security Resources"
  for namespace in "${TARGET_NAMESPACES[@]}"; do
    ensure_namespace "${namespace}"
    kubectl label namespace "${namespace}" istio-injection=enabled --overwrite
  done
}

install_security_chart() {
  section "Install RecSys Security Chart"
  helm upgrade --install recsys-security "${ROOT_DIR}/infra/helm/recsys-security" \
    --namespace recsys-security \
    --create-namespace \
    --wait \
    --timeout 5m
}

wait_external_secrets() {
  section "Wait For Vault-Synced Kubernetes Secrets"
  local pairs=(
    "recsys-dataflow/recsys-data-platform-secret"
    "experiment-tracking/recsys-mlflow-secrets"
    "kubeflow/recsys-mlops-runtime"
    "kserve-triton-inference/recsys-kserve-minio"
    "api-serving/recsys-gateway-basic-auth"
    "observability/recsys-gateway-basic-auth"
  )
  for pair in "${pairs[@]}"; do
    local namespace="${pair%%/*}"
    local name="${pair##*/}"
    kubectl wait --for=condition=Ready externalsecret/"${name}" -n "${namespace}" --timeout="${WAIT_TIMEOUT}"
    kubectl get secret "${name}" -n "${namespace}" >/dev/null
  done
}

install_external_secrets
install_vault
configure_vault_kubernetes_auth
seed_vault_secrets
install_istio
stabilize_external_secrets
prepare_namespaces_for_mesh
install_security_chart
wait_external_secrets
