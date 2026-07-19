#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-}"

PROJECT_ID="${GCP_PROJECT_ID:-${PROJECT_ID:-fsds-coursework}}"
ZONE="${GKE_ZONE:-${ZONE:-asia-southeast1-b}}"
CLUSTER="${GKE_CLUSTER:-${CLUSTER:-recsys-mlops-gke}}"
STATE_FILE="${GCP_POWER_STATE_FILE:-.gcp-services-power-state.env}"
WAIT_TIMEOUT="${GCP_SERVICES_WAIT_TIMEOUT:-900s}"
SKIP_SMOKE="${GCP_SERVICES_SKIP_SMOKE:-0}"
REQUIRE_AB_TEST="${GCP_SERVICES_REQUIRE_AB_TEST:-1}"
INSTALL_CI="${GCP_SERVICES_INSTALL_CI:-1}"
KEDA_HTTP_REPLICAS="${GCP_SERVICES_KEDA_HTTP_REPLICAS:-1}"
RESTORE_DATA_PLATFORM_CONFIG="${GCP_SERVICES_RESTORE_DATA_PLATFORM_CONFIG:-1}"
REALTIME_E2E_ENABLED="${GCP_SERVICES_REALTIME_E2E_ENABLED:-true}"
RETRAIN_PSI_THRESHOLD="${GCP_SERVICES_RETRAIN_PSI_THRESHOLD:-0.15}"
CI_NAMESPACE="${GCP_CI_NAMESPACE:-ci}"
CI_RELEASE="${GCP_CI_RELEASE:-recsys-ci}"
CI_VALUES_FILE="${GCP_CI_VALUES_FILE:-infra/helm/recsys-ci/values-gke.yaml}"
CI_HELM_TIMEOUT="${GCP_CI_HELM_TIMEOUT:-15m}"
SMOKE_JENKINS_PORT="${GCP_SERVICES_SMOKE_JENKINS_PORT:-28090}"
SMOKE_AIRFLOW_PORT="${GCP_SERVICES_SMOKE_AIRFLOW_PORT:-28080}"
SMOKE_DATAHUB_GMS_PORT="${GCP_SERVICES_SMOKE_DATAHUB_GMS_PORT:-28088}"
SMOKE_DATAHUB_FRONTEND_PORT="${GCP_SERVICES_SMOKE_DATAHUB_FRONTEND_PORT:-29002}"
SMOKE_PROMETHEUS_PORT="${GCP_SERVICES_SMOKE_PROMETHEUS_PORT:-29090}"
SMOKE_GRAFANA_PORT="${GCP_SERVICES_SMOKE_GRAFANA_PORT:-23000}"

CPU_NODE_POOL="${GCP_CPU_NODE_POOL:-recsys-mlops-cpu}"
ML_NODE_POOL="${GCP_ML_NODE_POOL:-recsys-mlops-ml-system}"
GPU_NODE_POOL="${GCP_GPU_NODE_POOL:-recsys-mlops-gpu}"

DEFAULT_CPU_NODES="${GCP_CPU_NODES:-1}"
DEFAULT_CPU_MIN_NODES="${GCP_CPU_MIN_NODES:-${DEFAULT_CPU_NODES}}"
DEFAULT_CPU_MAX_NODES="${GCP_CPU_MAX_NODES:-3}"
DEFAULT_ML_NODES="${GCP_ML_NODES:-1}"
DEFAULT_ML_MIN_NODES="${GCP_ML_MIN_NODES:-${DEFAULT_ML_NODES}}"
DEFAULT_ML_MAX_NODES="${GCP_ML_MAX_NODES:-1}"
DEFAULT_GPU_NODES="${GCP_GPU_NODES:-0}"
DEFAULT_GPU_MIN_NODES="${GCP_GPU_MIN_NODES:-${DEFAULT_GPU_NODES}}"
DEFAULT_GPU_MAX_NODES="${GCP_GPU_MAX_NODES:-1}"

usage() {
  cat <<USAGE
Usage:
  $0 down      Scale GKE node pools to 0 and keep PVC/PV data.
  $0 up        Restore node pools, wait services Ready, and run smoke checks.
  $0 status    Print node pools, PVCs, and non-running pods.

Environment overrides:
  GCP_PROJECT_ID=${PROJECT_ID}
  GKE_ZONE=${ZONE}
  GKE_CLUSTER=${CLUSTER}
  GCP_CPU_NODES=${DEFAULT_CPU_NODES}
  GCP_ML_NODES=${DEFAULT_ML_NODES}
  GCP_GPU_NODES=${DEFAULT_GPU_NODES}
  GCP_SERVICES_SKIP_SMOKE=${SKIP_SMOKE}
  GCP_SERVICES_INSTALL_CI=${INSTALL_CI}
  GCP_SERVICES_KEDA_HTTP_REPLICAS=${KEDA_HTTP_REPLICAS}
  GCP_SERVICES_RESTORE_DATA_PLATFORM_CONFIG=${RESTORE_DATA_PLATFORM_CONFIG}

State file:
  ${STATE_FILE}
USAGE
}

require_tools() {
  command -v gcloud >/dev/null
  command -v kubectl >/dev/null
  command -v curl >/dev/null
  if [[ "${INSTALL_CI}" == "1" ]]; then
    command -v helm >/dev/null
  fi
}

cluster_args() {
  printf -- '--project=%s --zone=%s' "${PROJECT_ID}" "${ZONE}"
}

get_credentials() {
  gcloud container clusters get-credentials "${CLUSTER}" --zone "${ZONE}" --project "${PROJECT_ID}" >/dev/null
}

pool_exists() {
  local pool="$1"
  gcloud container node-pools describe "${pool}" \
    --cluster "${CLUSTER}" \
    --zone "${ZONE}" \
    --project "${PROJECT_ID}" >/dev/null 2>&1
}

pool_value() {
  local pool="$1"
  local expr="$2"
  gcloud container node-pools describe "${pool}" \
    --cluster "${CLUSTER}" \
    --zone "${ZONE}" \
    --project "${PROJECT_ID}" \
    --format="value(${expr})" 2>/dev/null || true
}

safe_int() {
  local value="$1"
  local fallback="$2"
  if [[ "${value}" =~ ^[0-9]+$ ]]; then
    printf '%s' "${value}"
  else
    printf '%s' "${fallback}"
  fi
}

record_pool_state() {
  local key="$1"
  local pool="$2"
  local default_nodes="$3"
  local default_min="$4"
  local default_max="$5"

  if ! pool_exists "${pool}"; then
    printf '%s_EXISTS=0\n' "${key}" >>"${STATE_FILE}"
    return 0
  fi

  local current min max
  current="$(safe_int "$(pool_value "${pool}" "currentNodeCount")" "${default_nodes}")"
  min="$(safe_int "$(pool_value "${pool}" "autoscaling.minNodeCount")" "${default_min}")"
  max="$(safe_int "$(pool_value "${pool}" "autoscaling.maxNodeCount")" "${default_max}")"

  if (( max < min )); then
    max="${min}"
  fi
  if (( max < current )); then
    max="${current}"
  fi

  {
    printf '%s_EXISTS=1\n' "${key}"
    printf '%s_POOL=%q\n' "${key}" "${pool}"
    printf '%s_NODES=%q\n' "${key}" "${current}"
    printf '%s_MIN=%q\n' "${key}" "${min}"
    printf '%s_MAX=%q\n' "${key}" "${max}"
  } >>"${STATE_FILE}"
}

write_state() {
  {
    printf 'PROJECT_ID=%q\n' "${PROJECT_ID}"
    printf 'ZONE=%q\n' "${ZONE}"
    printf 'CLUSTER=%q\n' "${CLUSTER}"
    printf 'RECORDED_AT=%q\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${STATE_FILE}"
  record_pool_state CPU "${CPU_NODE_POOL}" "${DEFAULT_CPU_NODES}" "${DEFAULT_CPU_MIN_NODES}" "${DEFAULT_CPU_MAX_NODES}"
  record_pool_state ML "${ML_NODE_POOL}" "${DEFAULT_ML_NODES}" "${DEFAULT_ML_MIN_NODES}" "${DEFAULT_ML_MAX_NODES}"
  record_pool_state GPU "${GPU_NODE_POOL}" "${DEFAULT_GPU_NODES}" "${DEFAULT_GPU_MIN_NODES}" "${DEFAULT_GPU_MAX_NODES}"
}

set_pool_autoscaling() {
  local pool="$1"
  local min="$2"
  local max="$3"

  if (( max < min )); then
    max="${min}"
  fi
  if (( max < 1 )); then
    max=1
  fi

  gcloud container node-pools update "${pool}" \
    --cluster "${CLUSTER}" \
    --zone "${ZONE}" \
    --project "${PROJECT_ID}" \
    --enable-autoscaling \
    --min-nodes "${min}" \
    --max-nodes "${max}" \
    --quiet
}

disable_pool_autoscaling() {
  local pool="$1"

  gcloud container node-pools update "${pool}" \
    --cluster "${CLUSTER}" \
    --zone "${ZONE}" \
    --project "${PROJECT_ID}" \
    --no-enable-autoscaling \
    --quiet
}

resize_pool() {
  local pool="$1"
  local nodes="$2"

  gcloud container clusters resize "${CLUSTER}" \
    --node-pool "${pool}" \
    --num-nodes "${nodes}" \
    --zone "${ZONE}" \
    --project "${PROJECT_ID}" \
    --quiet
}

scale_pool_down() {
  local label="$1"
  local pool="$2"

  if ! pool_exists "${pool}"; then
    echo "Skip ${label}: node pool ${pool} does not exist."
    return 0
  fi

  local max
  max="$(safe_int "$(pool_value "${pool}" "autoscaling.maxNodeCount")" "1")"
  echo "Hibernate ${label}: ${pool} -> autoscaling=off, nodes=0 (recorded max=${max})"
  disable_pool_autoscaling "${pool}"
  resize_pool "${pool}" 0
}

scale_pool_up() {
  local label="$1"
  local pool="$2"
  local nodes="$3"
  local min="$4"
  local max="$5"

  if ! pool_exists "${pool}"; then
    echo "Skip ${label}: node pool ${pool} does not exist."
    return 0
  fi

  if (( nodes < min )); then
    nodes="${min}"
  fi
  if (( max < nodes )); then
    max="${nodes}"
  fi

  echo "Resume ${label}: ${pool} -> min=${min}, max=${max}, nodes=${nodes}"
  set_pool_autoscaling "${pool}" "${min}" "${max}"
  resize_pool "${pool}" "${nodes}"
}

load_state_or_defaults() {
  CPU_EXISTS=1
  CPU_POOL="${CPU_NODE_POOL}"
  CPU_NODES="${DEFAULT_CPU_NODES}"
  CPU_MIN="${DEFAULT_CPU_MIN_NODES}"
  CPU_MAX="${DEFAULT_CPU_MAX_NODES}"

  ML_EXISTS=1
  ML_POOL="${ML_NODE_POOL}"
  ML_NODES="${DEFAULT_ML_NODES}"
  ML_MIN="${DEFAULT_ML_MIN_NODES}"
  ML_MAX="${DEFAULT_ML_MAX_NODES}"

  GPU_EXISTS=1
  GPU_POOL="${GPU_NODE_POOL}"
  GPU_NODES="${DEFAULT_GPU_NODES}"
  GPU_MIN="${DEFAULT_GPU_MIN_NODES}"
  GPU_MAX="${DEFAULT_GPU_MAX_NODES}"

  if [[ -f "${STATE_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${STATE_FILE}"
  fi
}

print_status() {
  get_credentials
  echo "== Node pools =="
  gcloud container node-pools list \
    --cluster "${CLUSTER}" \
    --zone "${ZONE}" \
    --project "${PROJECT_ID}" \
    --format='table(name,status,autoscaling.enabled,autoscaling.minNodeCount,autoscaling.maxNodeCount,version)'
  echo
  echo "== Nodes =="
  kubectl get nodes -L cloud.google.com/gke-nodepool,recsys.ai/workload || true
  echo
  echo "== PVCs kept =="
  kubectl get pvc -A || true
  echo
  echo "== Pods not Running/Succeeded =="
  kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded || true
}

PORT_FORWARD_PIDS=()

cleanup_port_forwards() {
  local pid
  for pid in "${PORT_FORWARD_PIDS[@]:-}"; do
    if kill -0 "${pid}" >/dev/null 2>&1; then
      kill "${pid}" >/dev/null 2>&1 || true
    fi
  done
}

start_port_forward() {
  local namespace="$1"
  local resource="$2"
  local local_port="$3"
  local remote_port="$4"
  local log_file="/tmp/recsys-gcp-power-port-forward-${namespace}-${resource//\//-}-${local_port}.log"

  kubectl -n "${namespace}" port-forward "${resource}" "${local_port}:${remote_port}" >"${log_file}" 2>&1 &
  PORT_FORWARD_PIDS+=("$!")
}

wait_http() {
  local label="$1"
  local url="$2"
  local attempts="${3:-60}"
  local delay="${4:-2}"
  local i

  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      echo "${label}: OK"
      return 0
    fi
    sleep "${delay}"
  done
  echo "${label}: failed to reach ${url}" >&2
  return 1
}

wait_rollout_all() {
  local namespace="$1"

  if ! kubectl get namespace "${namespace}" >/dev/null 2>&1; then
    return 0
  fi

  echo "Wait rollouts in namespace ${namespace}"
  local kind
  local resources
  for kind in deployment statefulset daemonset; do
    resources="$(kubectl get "${kind}" -n "${namespace}" -o name 2>/dev/null || true)"
    if [[ -n "${resources}" ]]; then
      # Resource names from kubectl are newline-delimited and do not contain shell metacharacters.
      # shellcheck disable=SC2086
      kubectl rollout status -n "${namespace}" --timeout="${WAIT_TIMEOUT}" ${resources}
    fi
  done
}

wait_endpoint_address() {
  local namespace="$1"
  local endpoint="$2"
  local label="$3"
  local attempts="${4:-60}"
  local delay="${5:-2}"
  local i

  if ! kubectl get endpoints -n "${namespace}" "${endpoint}" >/dev/null 2>&1; then
    return 0
  fi

  echo "Wait endpoint ${namespace}/${endpoint} for ${label}"
  for ((i = 1; i <= attempts; i++)); do
    if kubectl get endpoints -n "${namespace}" "${endpoint}" \
      -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null | grep -q .; then
      return 0
    fi
    sleep "${delay}"
  done

  echo "Timed out waiting for endpoint ${namespace}/${endpoint} (${label})" >&2
  kubectl get endpoints -n "${namespace}" "${endpoint}" -o wide >&2 || true
  return 1
}

install_ci_stack() {
  if [[ "${INSTALL_CI}" != "1" ]]; then
    return 0
  fi

  echo "Install/update Jenkins CI stack in namespace ${CI_NAMESPACE}"
  helm upgrade --install "${CI_RELEASE}" infra/helm/recsys-ci \
    --namespace "${CI_NAMESPACE}" \
    --create-namespace \
    -f "${CI_VALUES_FILE}" \
    --wait \
    --timeout "${CI_HELM_TIMEOUT}"
}

scale_deploy_if_exists() {
  local namespace="$1"
  local deployment="$2"
  local replicas="$3"

  if ! kubectl get deploy -n "${namespace}" "${deployment}" >/dev/null 2>&1; then
    return 0
  fi

  local current
  current="$(kubectl get deploy -n "${namespace}" "${deployment}" -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
  if [[ "${current}" == "${replicas}" ]]; then
    return 0
  fi

  echo "Normalize ${namespace}/${deployment}: replicas ${current:-unknown} -> ${replicas}"
  kubectl scale deploy -n "${namespace}" "${deployment}" --replicas="${replicas}"
}

normalize_keda_http_addon() {
  if ! [[ "${KEDA_HTTP_REPLICAS}" =~ ^[0-9]+$ ]]; then
    echo "GCP_SERVICES_KEDA_HTTP_REPLICAS must be an integer, got: ${KEDA_HTTP_REPLICAS}" >&2
    return 2
  fi

  # One replica keeps the KEDA HTTP add-on fully available for coursework proof while
  # preserving enough schedulable CPU for KFP component pods and the RayJob launcher.
  if kubectl get scaledobject -n keda keda-add-ons-http-interceptor >/dev/null 2>&1; then
    echo "Normalize keda/scaledobject.keda-add-ons-http-interceptor: minReplicaCount -> ${KEDA_HTTP_REPLICAS}"
    kubectl patch scaledobject -n keda keda-add-ons-http-interceptor --type merge \
      -p "{\"spec\":{\"minReplicaCount\":${KEDA_HTTP_REPLICAS}}}"
  fi
  if kubectl get hpa -n keda keda-hpa-keda-add-ons-http-interceptor >/dev/null 2>&1; then
    kubectl patch hpa -n keda keda-hpa-keda-add-ons-http-interceptor --type merge \
      -p "{\"spec\":{\"minReplicas\":${KEDA_HTTP_REPLICAS}}}" >/dev/null
  fi
  scale_deploy_if_exists keda keda-add-ons-http-external-scaler "${KEDA_HTTP_REPLICAS}"
  scale_deploy_if_exists keda keda-add-ons-http-interceptor "${KEDA_HTTP_REPLICAS}"
}

restore_data_platform_runtime_config() {
  if [[ "${RESTORE_DATA_PLATFORM_CONFIG}" != "1" ]]; then
    return 0
  fi
  if ! kubectl get configmap -n recsys-dataflow recsys-data-platform-config >/dev/null 2>&1; then
    return 0
  fi

  echo "Restore data-platform runtime config: REALTIME_E2E_ENABLED=${REALTIME_E2E_ENABLED}, RETRAIN_PSI_THRESHOLD=${RETRAIN_PSI_THRESHOLD}"
  kubectl -n recsys-dataflow patch configmap recsys-data-platform-config --type merge \
    -p "{\"data\":{\"REALTIME_E2E_ENABLED\":\"${REALTIME_E2E_ENABLED}\",\"RETRAIN_PSI_THRESHOLD\":\"${RETRAIN_PSI_THRESHOLD}\"}}"
}

normalize_gcp_runtime() {
  normalize_keda_http_addon
  restore_data_platform_runtime_config
}

enable_mesh_injection_for_namespace() {
  local namespace="$1"

  if ! kubectl get namespace "${namespace}" >/dev/null 2>&1; then
    return 0
  fi

  local injection
  injection="$(kubectl get namespace "${namespace}" -o jsonpath='{.metadata.labels.istio-injection}' 2>/dev/null || true)"
  if [[ "${injection}" == "enabled" ]]; then
    return 0
  fi

  echo "Enable Istio sidecar injection for namespace ${namespace}"
  kubectl label namespace "${namespace}" istio-injection=enabled --overwrite
}

ensure_ingress_nginx_mesh() {
  if ! kubectl get deploy -n ingress-nginx ingress-nginx-controller >/dev/null 2>&1; then
    return 0
  fi

  enable_mesh_injection_for_namespace ingress-nginx

  local containers
  containers="$(kubectl get pods -n ingress-nginx \
    -l app.kubernetes.io/component=controller,app.kubernetes.io/instance=ingress-nginx,app.kubernetes.io/name=ingress-nginx \
    -o jsonpath='{range .items[*]}{.spec.containers[*].name}{" "}{.spec.initContainers[*].name}{"\n"}{end}' 2>/dev/null || true)"
  if grep -q 'istio-proxy' <<<"${containers}"; then
    return 0
  fi

  echo "Restart ingress-nginx controller to inject Istio sidecar for mTLS upstreams."
  kubectl patch deployment ingress-nginx-controller -n ingress-nginx --type merge -p '{
    "spec": {
      "template": {
        "metadata": {
          "annotations": {
            "sidecar.istio.io/inject": "true",
            "traffic.sidecar.istio.io/includeInboundPorts": ""
          }
        }
      }
    }
  }'
  kubectl rollout restart deployment/ingress-nginx-controller -n ingress-nginx
  kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx --timeout="${WAIT_TIMEOUT}"
}

patch_datahub_gms_probe() {
  if ! kubectl get deploy -n datahub datahub-datahub-gms >/dev/null 2>&1; then
    return 0
  fi

  local readiness_path
  local liveness_path
  readiness_path="$(kubectl get deploy -n datahub datahub-datahub-gms -o jsonpath='{.spec.template.spec.containers[?(@.name=="datahub-gms")].readinessProbe.httpGet.path}' 2>/dev/null || true)"
  liveness_path="$(kubectl get deploy -n datahub datahub-datahub-gms -o jsonpath='{.spec.template.spec.containers[?(@.name=="datahub-gms")].livenessProbe.httpGet.path}' 2>/dev/null || true)"

  if [[ "${readiness_path}" == "/config" && "${liveness_path}" == "/config" ]]; then
    return 0
  fi

  echo "Patch DataHub GMS probes: /health -> /config"
  kubectl patch deployment datahub-datahub-gms -n datahub --type='json' \
    -p='[
      {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/httpGet/path","value":"/config"},
      {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/config"}
    ]'
}

ensure_realtime_kafka_topic() {
  if ! kubectl get deploy -n recsys-dataflow kafka >/dev/null 2>&1; then
    return 0
  fi

  local topic
  local partitions
  topic="$(kubectl get configmap -n recsys-dataflow recsys-data-platform-config -o jsonpath='{.data.REALTIME_STREAM_TOPIC}' 2>/dev/null || true)"
  topic="${topic:-cdc.behavior_events}"
  partitions="$(kubectl get configmap -n recsys-dataflow recsys-data-platform-config -o jsonpath='{.data.REALTIME_STREAM_TOPIC_PARTITIONS}' 2>/dev/null || true)"
  partitions="${partitions:-4}"

  echo "Ensure realtime Kafka topic: ${topic}"
  kubectl exec -n recsys-dataflow deploy/kafka -- \
    kafka-topics --bootstrap-server localhost:29092 \
      --create --if-not-exists \
      --topic "${topic}" \
      --partitions "${partitions}" \
      --replication-factor 1 >/dev/null

  local current
  current="$(kubectl exec -n recsys-dataflow deploy/kafka -- kafka-topics --bootstrap-server localhost:29092 --describe --topic "${topic}" | sed -n 's/.*PartitionCount: \([0-9][0-9]*\).*/\1/p' | head -n 1)"
  if [[ "${current:-0}" -lt "${partitions}" ]]; then
    kubectl exec -n recsys-dataflow deploy/kafka -- \
      kafka-topics --bootstrap-server localhost:29092 --alter --topic "${topic}" --partitions "${partitions}" >/dev/null
  fi
}

flink_has_running_job() {
  kubectl exec -n recsys-dataflow deploy/flink-jobmanager -- \
    python -c 'import json, urllib.request; jobs=json.load(urllib.request.urlopen("http://localhost:8081/jobs/overview", timeout=10)); running=[job for job in jobs.get("jobs", []) if job.get("state") == "RUNNING"]; raise SystemExit(0 if len(running) >= 2 else 1)'
}

flink_cancel_duplicate_restarting_jobs() {
  kubectl exec -n recsys-dataflow deploy/flink-jobmanager -- \
    python -c 'import json, urllib.request; base="http://localhost:8081"; jobs=json.load(urllib.request.urlopen(base + "/jobs/overview", timeout=10)).get("jobs", []); running=[job for job in jobs if job.get("state") == "RUNNING"]; restarting=[job for job in jobs if job.get("state") == "RESTARTING"]; print("Flink RUNNING jobs:", len(running)); print("Flink duplicate RESTARTING jobs:", [(job.get("jid"), job.get("name")) for job in restarting]);
if len(running) < 2:
    raise SystemExit(0)
for job in restarting:
    req=urllib.request.Request(base + "/jobs/" + job["jid"] + "?mode=cancel", method="PATCH")
    urllib.request.urlopen(req, timeout=10).read()'
}

ensure_realtime_flink_running() {
  if ! kubectl get deploy -n recsys-dataflow realtime-flink-online-store >/dev/null 2>&1; then
    return 0
  fi
  if ! kubectl get deploy -n recsys-dataflow realtime-flink-offline-store >/dev/null 2>&1; then
    return 0
  fi

  if flink_has_running_job; then
    flink_cancel_duplicate_restarting_jobs || true
    return 0
  fi

  echo "Restart realtime Flink consumers because online/offline RUNNING jobs were not both found."
  kubectl rollout restart deployment/realtime-flink-online-store deployment/realtime-flink-offline-store -n recsys-dataflow
  kubectl rollout status deployment/realtime-flink-online-store -n recsys-dataflow --timeout="${WAIT_TIMEOUT}"
  kubectl rollout status deployment/realtime-flink-offline-store -n recsys-dataflow --timeout="${WAIT_TIMEOUT}"

  local i
  for ((i = 1; i <= 30; i++)); do
    if flink_has_running_job; then
      flink_cancel_duplicate_restarting_jobs || true
      return 0
    fi
    sleep 5
  done

  echo "Flink smoke failed: online/offline Flink jobs were not both RUNNING after consumer restart." >&2
  kubectl exec -n recsys-dataflow deploy/flink-jobmanager -- curl -s http://localhost:8081/jobs/overview || true
  echo >&2
  return 1
}

wait_ready_after_up() {
  get_credentials

  if (( CPU_NODES > 0 )); then
    kubectl wait --for=condition=Ready node \
      -l "cloud.google.com/gke-nodepool=${CPU_POOL}" \
      --timeout="${WAIT_TIMEOUT}"
  fi
  if (( ML_NODES > 0 )); then
    kubectl wait --for=condition=Ready node \
      -l "cloud.google.com/gke-nodepool=${ML_POOL}" \
      --timeout="${WAIT_TIMEOUT}"
  fi
  if (( GPU_NODES > 0 )) && pool_exists "${GPU_POOL}"; then
    kubectl wait --for=condition=Ready node \
      -l "cloud.google.com/gke-nodepool=${GPU_POOL}" \
      --timeout="${WAIT_TIMEOUT}" || true
  fi

  ensure_ingress_nginx_mesh

  local system_namespaces=(
    cert-manager
    external-secrets
    istio-system
    keda
    kserve
    ingress-nginx
  )
  for namespace in "${system_namespaces[@]}"; do
    wait_rollout_all "${namespace}"
  done

  wait_endpoint_address ingress-nginx ingress-nginx-controller-admission "NGINX validating webhook"
  normalize_gcp_runtime
  install_ci_stack

  local app_namespaces=(
    "${CI_NAMESPACE}"
    experiment-tracking
    recsys-dataflow
    kubeflow
    kserve-triton-inference
    api-serving
    observability
    datahub
  )
  for namespace in "${app_namespaces[@]}"; do
    if [[ "${namespace}" == "datahub" ]]; then
      patch_datahub_gms_probe
    fi
    wait_rollout_all "${namespace}"
  done

  ensure_realtime_kafka_topic
  ensure_realtime_flink_running

  if kubectl get inferenceservice -n kserve-triton-inference recsys-bst-triton >/dev/null 2>&1; then
    kubectl wait --for=condition=Ready inferenceservice/recsys-bst-triton \
      -n kserve-triton-inference \
      --timeout="${WAIT_TIMEOUT}"
  fi
  if kubectl get inferenceservice -n kserve-triton-inference recsys-bst-triton-candidate >/dev/null 2>&1; then
    kubectl wait --for=condition=Ready inferenceservice/recsys-bst-triton-candidate \
      -n kserve-triton-inference \
      --timeout="${WAIT_TIMEOUT}" || true
  fi
}

assert_ray_launcher_headroom() {
  local namespace="kubeflow"
  local pod="recsys-ray-launcher-headroom-smoke"

  if ! kubectl get namespace "${namespace}" >/dev/null 2>&1; then
    return 0
  fi

  echo "== Smoke: schedulable headroom for KFP/Ray launcher =="
  kubectl delete pod -n "${namespace}" "${pod}" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${pod}
  namespace: ${namespace}
  annotations:
    sidecar.istio.io/inject: "false"
spec:
  restartPolicy: Never
  securityContext:
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: smoke
      image: busybox:1.36
      command: ["sh", "-c", "echo ray-launcher-headroom-ok"]
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop:
            - ALL
        runAsNonRoot: true
        runAsUser: 65534
      resources:
        requests:
          cpu: 500m
          memory: 200Mi
        limits:
          cpu: 500m
          memory: 200Mi
YAML
  kubectl wait -n "${namespace}" --for=condition=PodScheduled "pod/${pod}" --timeout=120s
  kubectl wait -n "${namespace}" --for=jsonpath='{.status.phase}'=Succeeded "pod/${pod}" --timeout=180s
  kubectl delete pod -n "${namespace}" "${pod}" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}

smoke_after_up() {
  if [[ "${SKIP_SMOKE}" == "1" ]]; then
    echo "Skip smoke checks because GCP_SERVICES_SKIP_SMOKE=1."
    return 0
  fi

  trap cleanup_port_forwards EXIT

  echo "== Smoke: no Pending/Failed pods =="
  local non_running
  non_running="$(
    kubectl get pods -A \
      --field-selector=status.phase!=Running,status.phase!=Succeeded \
      --no-headers \
      --show-labels 2>/dev/null \
      | awk '$NF !~ /(^|,)workflows.argoproj.io\/completed=true(,|$)/ {print}' \
      || true
  )"
  if [[ -n "${non_running}" ]]; then
    printf '%s\n' "${non_running}"
    return 1
  fi
  echo "All service pods are Running or Succeeded; completed Argo/KFP workflow artifacts are ignored."

  if kubectl get deploy -n api-serving recsys-online-feature-api >/dev/null 2>&1; then
    echo "== Smoke: online feature API =="
    kubectl exec -n api-serving deploy/recsys-online-feature-api -c api -- \
      python -c 'import json, urllib.request; body={"user_id":1001,"candidate_item_ids":[1,2,3,4,5],"top_k":5}; data=json.dumps(body).encode(); req=urllib.request.Request("http://127.0.0.1:8080/online-features", data=data, headers={"Content-Type":"application/json"}, method="POST"); r=urllib.request.urlopen(req, timeout=30); print(r.status); print(r.read().decode()[:500])'
  fi

  if kubectl get deploy -n api-serving recsys-api-serving >/dev/null 2>&1; then
    echo "== Smoke: recommendation API =="
    kubectl exec -n api-serving deploy/recsys-api-serving -c api -- \
      python -c 'import json, urllib.request; body={"user_id":1001,"candidate_item_ids":[1,2,3,4,5,6,7,8,9,10],"top_k":5}; data=json.dumps(body).encode(); req=urllib.request.Request("http://127.0.0.1:8080/recommendations", data=data, headers={"Content-Type":"application/json"}, method="POST"); r=urllib.request.urlopen(req, timeout=30); print(r.status); print(r.read().decode()[:500])'

    echo "== Smoke: A/B traffic split =="
    local ab_enabled
    ab_enabled="$(kubectl get configmap -n api-serving recsys-api-serving -o jsonpath='{.data.AB_TEST_ENABLED}' 2>/dev/null || true)"
    if [[ "${ab_enabled}" == "1" ]]; then
      kubectl exec -i -n api-serving deploy/recsys-api-serving -c api -- python - <<'PY'
import collections
import json
import time
import urllib.error

import urllib.request

counts = collections.Counter()
base = {"candidate_item_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], "top_k": 5}
for user_id in range(1001, 1201):
    payload = dict(base, user_id=user_id)
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        "http://127.0.0.1:8080/recommendations",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                counts.update([json.loads(response.read().decode()).get("ab_variant") or "none"])
            break
        except urllib.error.HTTPError:
            if attempt == 3:
                raise
            time.sleep(0.5 * attempt)
print(json.dumps(counts, sort_keys=True))
assert counts["candidate"] > 0 and counts["control"] > 0, counts
PY
    elif [[ "${REQUIRE_AB_TEST}" == "1" ]]; then
      echo "A/B smoke failed: AB_TEST_ENABLED is not 1. Set GCP_SERVICES_REQUIRE_AB_TEST=0 to allow a non-A/B resume." >&2
      return 1
    else
      echo "A/B smoke skipped because AB_TEST_ENABLED is not 1."
    fi
  fi

  assert_ray_launcher_headroom

  if kubectl get deploy -n recsys-dataflow flink-jobmanager >/dev/null 2>&1; then
    echo "== Smoke: Flink overview =="
    flink_cancel_duplicate_restarting_jobs || true
    kubectl exec -n recsys-dataflow deploy/flink-jobmanager -- \
      curl -s http://localhost:8081/jobs/overview || true
    echo
    flink_has_running_job
  fi

  if [[ "${INSTALL_CI}" == "1" ]] && kubectl get deploy -n "${CI_NAMESPACE}" recsys-jenkins >/dev/null 2>&1; then
    echo "== Smoke: Jenkins CI UI =="
    start_port_forward "${CI_NAMESPACE}" svc/recsys-jenkins "${SMOKE_JENKINS_PORT}" 8080
    wait_http "Jenkins UI" "http://127.0.0.1:${SMOKE_JENKINS_PORT}/login"
  fi

  if kubectl get deploy -n recsys-dataflow airflow-webserver >/dev/null 2>&1; then
    echo "== Smoke: Airflow DAGs and UI health =="
    kubectl exec -n recsys-dataflow deploy/airflow-webserver -c airflow-webserver -- \
      test -f /opt/recsys/apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py
    start_port_forward recsys-dataflow svc/airflow-webserver "${SMOKE_AIRFLOW_PORT}" 8080
    wait_http "Airflow UI /health" "http://127.0.0.1:${SMOKE_AIRFLOW_PORT}/health"
  fi

  if kubectl get deploy -n datahub datahub-datahub-gms >/dev/null 2>&1; then
    echo "== Smoke: DataHub GMS and frontend UI =="
    start_port_forward datahub svc/datahub-datahub-gms "${SMOKE_DATAHUB_GMS_PORT}" 8080
    wait_http "DataHub GMS /config" "http://127.0.0.1:${SMOKE_DATAHUB_GMS_PORT}/config"
    if kubectl get svc -n datahub datahub-datahub-frontend >/dev/null 2>&1; then
      start_port_forward datahub svc/datahub-datahub-frontend "${SMOKE_DATAHUB_FRONTEND_PORT}" 9002
      wait_http "DataHub frontend" "http://127.0.0.1:${SMOKE_DATAHUB_FRONTEND_PORT}/"
    fi
  fi

  if kubectl get deploy -n observability recsys-prometheus >/dev/null 2>&1; then
    echo "== Smoke: Prometheus metrics =="
    start_port_forward observability svc/recsys-prometheus "${SMOKE_PROMETHEUS_PORT}" 9090
    wait_http "Prometheus ready" "http://127.0.0.1:${SMOKE_PROMETHEUS_PORT}/-/ready"
    curl -fsS --get --data-urlencode 'query=sum(model_predictions_total)' \
      "http://127.0.0.1:${SMOKE_PROMETHEUS_PORT}/api/v1/query"
    echo
  fi

  if kubectl get deploy -n observability recsys-grafana >/dev/null 2>&1; then
    echo "== Smoke: Grafana UI and provisioned dashboards =="
    start_port_forward observability svc/recsys-grafana "${SMOKE_GRAFANA_PORT}" 3000
    wait_http "Grafana health" "http://127.0.0.1:${SMOKE_GRAFANA_PORT}/api/health"
    kubectl get configmap -n observability -l app.kubernetes.io/name=recsys-grafana >/dev/null
  fi
}

hibernate_down() {
  get_credentials
  echo "Recording live node-pool state to ${STATE_FILE}"
  write_state
  echo "PVC/PV data will be kept. This command does not delete namespaces, Helm releases, PVCs, or PVs."
  kubectl get pvc -A || true
  scale_pool_down CPU "${CPU_NODE_POOL}"
  scale_pool_down ML "${ML_NODE_POOL}"
  scale_pool_down GPU "${GPU_NODE_POOL}"
  echo "GCP services are hibernating. Run '$0 up' to restore node pools and wait services Ready."
}

resume_up() {
  get_credentials
  if [[ ! -f "${STATE_FILE}" ]]; then
    echo "No ${STATE_FILE} found; recording current node-pool state before idempotent resume."
    write_state
  fi
  load_state_or_defaults

  if [[ "${CPU_EXISTS:-1}" == "1" ]]; then
    scale_pool_up CPU "${CPU_POOL}" "${CPU_NODES}" "${CPU_MIN}" "${CPU_MAX}"
  fi
  if [[ "${ML_EXISTS:-1}" == "1" ]]; then
    scale_pool_up ML "${ML_POOL}" "${ML_NODES}" "${ML_MIN}" "${ML_MAX}"
  fi
  if [[ "${GPU_EXISTS:-1}" == "1" ]]; then
    scale_pool_up GPU "${GPU_POOL}" "${GPU_NODES}" "${GPU_MIN}" "${GPU_MAX}"
  fi

  wait_ready_after_up
  smoke_after_up
  echo "GCP services are back up and PVC-backed data was preserved."
}

require_tools

case "${ACTION}" in
  down)
    hibernate_down
    ;;
  up)
    resume_up
    ;;
  status)
    print_status
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
