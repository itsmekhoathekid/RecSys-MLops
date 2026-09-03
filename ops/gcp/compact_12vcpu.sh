#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-help}"
ROOT="$(git rev-parse --show-toplevel)"
TF_DIR="${ROOT}/infra/terraform/gcp"
PROFILE_TFVARS="${TF_DIR}/profiles/compact-12vcpu.tfvars"
STATE_DIR="${COMPACT_STATE_DIR:-${ROOT}/.compact-12vcpu}"
PROJECT_ID="$(python3 "${ROOT}/jenkins/python/configuration.py" gcp projectId)"
ZONE="$(python3 "${ROOT}/jenkins/python/configuration.py" gcp zone)"
REGION="$(python3 "${ROOT}/jenkins/python/configuration.py" gcp region)"
CLUSTER="$(python3 "${ROOT}/jenkins/python/configuration.py" gcp cluster)"
CPU_POOL="${GCP_CPU_NODE_POOL:-recsys-mlops-cpu}"
ML_POOL="${GCP_ML_NODE_POOL:-recsys-mlops-ml-system}"
PLAN_FILE="${STATE_DIR}/compact-12vcpu.tfplan"
PLAN_JSON="${STATE_DIR}/compact-12vcpu.tfplan.json"
PVC_BASELINE="${STATE_DIR}/pvc-identities.json"
SNAPSHOT_LOCATION="${COMPACT_SNAPSHOT_LOCATION:-${REGION}}"
COMPACT_COMPONENTS="online_feature_api,inference_api,kserve,rag_api,feature_rag_mcp,context_agent,recommendation_mcp,recommendation_agent,coordinator_agent,ci_config"

usage() {
  cat <<'USAGE'
Usage: ops/gcp/compact_12vcpu.sh plan|snapshot|up|verify|down|restore-standard

The command never deletes Helm releases, namespaces, PVCs, PVs, disks, buckets,
KMS keys or the GKE cluster. Set COMPACT_BASE_TFVARS when the standard
terraform.tfvars is not in the main worktree. Set COMPACT_TFSTATE_BUCKET to
override backend discovery. Jenkins is triggered during up when
JENKINS_URL/JENKINS_USER/JENKINS_TOKEN are available.
USAGE
}

require_tools() {
  local tool
  for tool in gcloud kubectl helm terraform jq python3 git; do
    command -v "${tool}" >/dev/null || {
      echo "missing required tool: ${tool}" >&2
      exit 2
    }
  done
}

require_compact_branch() {
  local branch
  branch="$(git -C "${ROOT}" branch --show-current)"
  [[ "${branch}" == "feats/gcp-12vcpu-compact" ]] || {
    echo "refusing compact mutation from branch ${branch:-detached}" >&2
    exit 2
  }
}

get_credentials() {
  gcloud container clusters get-credentials "${CLUSTER}" \
    --zone "${ZONE}" --project "${PROJECT_ID}" >/dev/null
}

base_tfvars() {
  if [[ -n "${COMPACT_BASE_TFVARS:-}" ]]; then
    printf '%s\n' "${COMPACT_BASE_TFVARS}"
    return
  fi
  if [[ -f "${TF_DIR}/terraform.tfvars" ]]; then
    printf '%s\n' "${TF_DIR}/terraform.tfvars"
    return
  fi
  local path branch main_path=""
  while IFS= read -r line; do
    case "${line}" in
      "worktree "*) path="${line#worktree }" ;;
      "branch refs/heads/main") main_path="${path}" ;;
    esac
  done < <(git -C "${ROOT}" worktree list --porcelain)
  [[ -n "${main_path}" && -f "${main_path}/infra/terraform/gcp/terraform.tfvars" ]] || {
    echo "set COMPACT_BASE_TFVARS to the existing production terraform.tfvars" >&2
    exit 2
  }
  printf '%s\n' "${main_path}/infra/terraform/gcp/terraform.tfvars"
}

tfstate_bucket() {
  if [[ -n "${COMPACT_TFSTATE_BUCKET:-}" ]]; then
    printf '%s\n' "${COMPACT_TFSTATE_BUCKET#gs://}"
    return
  fi
  local local_state candidate
  local_state="$(git -C "${ROOT}" worktree list --porcelain | awk '
    /^worktree / {path=substr($0,10)}
    /^branch refs\/heads\/main$/ {print path "/infra/terraform/gcp/.terraform/terraform.tfstate"}
  ')"
  if [[ -f "${local_state}" ]]; then
    candidate="$(jq -r '.backend.config.bucket // empty' "${local_state}")"
    [[ -n "${candidate}" ]] && { printf '%s\n' "${candidate}"; return; }
  fi
  candidate="$(gcloud storage buckets list --project "${PROJECT_ID}" \
    --filter='name~tfstate AND -name~backup' --format='value(name)' | head -n 1)"
  [[ -n "${candidate}" ]] || {
    echo "unable to discover Terraform state bucket; set COMPACT_TFSTATE_BUCKET" >&2
    exit 2
  }
  printf '%s\n' "${candidate#gs://}"
}

preflight() {
  local billing global_limit e2_limit n2_limit
  billing="$(gcloud billing projects describe "${PROJECT_ID}" --format='value(billingEnabled)')"
  [[ "${billing}" == "True" || "${billing}" == "true" ]] || {
    echo "billing is not enabled for ${PROJECT_ID}" >&2
    exit 2
  }
  global_limit="$(gcloud compute project-info describe --project "${PROJECT_ID}" --format=json | jq -r '.quotas[] | select(.metric=="CPUS_ALL_REGIONS") | .limit')"
  read -r e2_limit n2_limit < <(gcloud compute regions describe "${REGION}" --project "${PROJECT_ID}" --format=json | jq -r '[.quotas[] | select(.metric=="E2_CPUS" or .metric=="N2_CPUS")] | (map(select(.metric=="E2_CPUS"))[0].limit|tostring) + " " + (map(select(.metric=="N2_CPUS"))[0].limit|tostring)')
  python3 - "${global_limit}" "${e2_limit}" "${n2_limit}" <<'PY'
import sys
global_limit, e2, n2 = map(float, sys.argv[1:])
if global_limit < 12 or e2 < 4 or n2 < 8:
    raise SystemExit(f"quota insufficient: global={global_limit:g}, E2={e2:g}, N2={n2:g}")
print(f"quota preflight OK: global={global_limit:g}, E2={e2:g}, N2={n2:g}")
PY
}

record_pvcs() {
  local output="${1:-${PVC_BASELINE}}"
  mkdir -p "${STATE_DIR}"
  kubectl get pvc -A -o json >"${STATE_DIR}/pvcs.json"
  kubectl get pv -o json >"${STATE_DIR}/pvs.json"
  python3 - "${STATE_DIR}/pvcs.json" "${STATE_DIR}/pvs.json" "${output}" <<'PY'
import json, sys
pvc_path, pv_path, output = sys.argv[1:]
pvcs = json.load(open(pvc_path, encoding="utf-8"))["items"]
pvs = {item["metadata"]["name"]: item for item in json.load(open(pv_path, encoding="utf-8"))["items"]}
rows = []
for pvc in pvcs:
    pv_name = pvc.get("spec", {}).get("volumeName", "")
    pv = pvs.get(pv_name, {})
    rows.append({
        "namespace": pvc["metadata"]["namespace"],
        "name": pvc["metadata"]["name"],
        "uid": pvc["metadata"]["uid"],
        "pv": pv_name,
        "volumeHandle": pv.get("spec", {}).get("csi", {}).get("volumeHandle", ""),
    })
json.dump(sorted(rows, key=lambda row: (row["namespace"], row["name"])), open(output, "w", encoding="utf-8"), indent=2)
PY
  echo "recorded $(jq length "${output}") PVC identities in ${output}"
}

snapshot() {
  get_credentials
  record_pvcs
  local stamp manifest bucket uri row handle disk_zone disk snapshot_name
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  manifest="${STATE_DIR}/snapshots-${stamp}.jsonl"
  : >"${manifest}"
  while IFS= read -r row; do
    handle="$(jq -r '.volumeHandle' <<<"${row}")"
    [[ "${handle}" == */zones/*/disks/* ]] || continue
    disk_zone="$(awk -F/ '{print $(NF-2)}' <<<"${handle}")"
    disk="${handle##*/}"
    snapshot_name="$(printf 'compact-%s-%s' "$(printf '%s' "${stamp}" | tr '[:upper:]' '[:lower:]')" "${disk}" | tr '_' '-' | cut -c1-63 | sed 's/-$//')"
    gcloud compute snapshots describe "${snapshot_name}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
      gcloud compute disks snapshot "${disk}" --zone "${disk_zone}" \
        --project "${PROJECT_ID}" --snapshot-names "${snapshot_name}" \
        --storage-location "${SNAPSHOT_LOCATION}" --quiet
    jq -cn --argjson pvc "${row}" --arg snapshot "${snapshot_name}" \
      '{pvc:$pvc,snapshot:$snapshot}' >>"${manifest}"
  done < <(jq -c '.[] | select(.namespace | test("^(vault|recsys-dataflow|experiment-tracking|ci|langfuse|agentregistry|kagent|kubeflow|observability|ate-system)$"))' "${PVC_BASELINE}")
  jq -s --arg project "${PROJECT_ID}" --arg created "${stamp}" \
    '{project:$project,createdAt:$created,volumes:.}' "${manifest}" >"${STATE_DIR}/snapshots-${stamp}.json"
  bucket="$(tfstate_bucket)"
  uri="gs://${bucket}/operations/compact-12vcpu/snapshots-${stamp}.json"
  gcloud storage cp "${STATE_DIR}/snapshots-${stamp}.json" "${uri}" >/dev/null
  printf '%s\n' "${uri}" >"${STATE_DIR}/latest-snapshot-uri"
  echo "snapshot manifest: ${uri}"
}

ensure_snapshot() {
  local uri=""
  if [[ "${COMPACT_FORCE_SNAPSHOT:-0}" != "1" && -f "${STATE_DIR}/latest-snapshot-uri" ]]; then
    uri="$(<"${STATE_DIR}/latest-snapshot-uri")"
    if [[ -n "${uri}" ]] && gcloud storage objects describe "${uri}" >/dev/null 2>&1; then
      echo "reusing completed snapshot manifest: ${uri}"
      record_pvcs
      return
    fi
  fi
  snapshot
}

terraform_plan() {
  mkdir -p "${STATE_DIR}"
  local tfvars bucket shared_data_dir
  tfvars="$(base_tfvars)"
  bucket="$(tfstate_bucket)"
  shared_data_dir="${COMPACT_TF_DATA_DIR:-$(dirname "${tfvars}")/.terraform}"
  if [[ -d "${shared_data_dir}" ]]; then
    export TF_DATA_DIR="${shared_data_dir}"
  fi
  terraform -chdir="${TF_DIR}" init -input=false -backend-config="bucket=${bucket}"
  terraform -chdir="${TF_DIR}" plan -input=false \
    -var-file="${tfvars}" -var-file="${PROFILE_TFVARS}" -out="${PLAN_FILE}"
  terraform -chdir="${TF_DIR}" show -json "${PLAN_FILE}" >"${PLAN_JSON}"
  python3 - "${PLAN_JSON}" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
replacements = []
for change in plan.get("resource_changes", []):
    actions = change["change"]["actions"]
    address = change["address"]
    resource_type = change["type"]
    if "delete" in actions:
        if resource_type != "google_container_node_pool" or "create" not in actions:
            raise SystemExit(f"unsafe delete in compact plan: {address} {actions}")
        replacements.append(address)
for token in ("google_container_cluster", "google_storage_bucket", "google_kms", "persistent_volume", "compute_disk"):
    for change in plan.get("resource_changes", []):
        if token in change["type"] and "delete" in change["change"]["actions"]:
            raise SystemExit(f"protected resource would be destroyed: {change['address']}")
print("plan policy OK; node-pool replacements:", ", ".join(replacements) or "none")
PY
}

helm_profile() {
  local release="$1" namespace="$2" chart="$3"
  local overlay="${ROOT}/${chart}/values-compact-12vcpu.yaml"
  local gcp_values="${ROOT}/${chart}/values-gcp.yaml"
  local current_values
  local helm_args=()
  helm status "${release}" -n "${namespace}" >/dev/null 2>&1 || return 0
  current_values="$(mktemp)"
  chmod 600 "${current_values}"
  helm get values "${release}" -n "${namespace}" -o yaml >"${current_values}"
  helm_args=(-f "${current_values}")
  [[ -f "${gcp_values}" ]] && helm_args+=(-f "${gcp_values}")
  helm_args+=(-f "${overlay}")
  case "${release}" in
    recsys-data-lakehouse)
      helm_args+=(--set-string "minio.storage=$(kubectl -n recsys-dataflow get pvc data-data-platform-minio-0 -o jsonpath='{.spec.resources.requests.storage}')")
      ;;
    recsys-source-store)
      helm_args+=(--set-string "sourcePostgres.storage=$(kubectl -n recsys-dataflow get pvc data-source-postgres-0 -o jsonpath='{.spec.resources.requests.storage}')")
      ;;
    recsys-event-stream)
      helm_args+=(
        --set-string "kafka.persistence.storage=$(kubectl -n recsys-dataflow get pvc kafka-data -o jsonpath='{.spec.resources.requests.storage}')"
        --set-string "zookeeper.persistence.storage=$(kubectl -n recsys-dataflow get pvc zookeeper-data -o jsonpath='{.spec.resources.requests.storage}')"
      )
      ;;
    recsys-airflow)
      helm_args+=(--set-string "airflowPostgres.storage=$(kubectl -n recsys-dataflow get pvc data-airflow-postgres-0 -o jsonpath='{.spec.resources.requests.storage}')")
      ;;
    recsys-analytics)
      helm_args+=(
        --set-string "catalog.storage=$(kubectl -n analytics get pvc data-recsys-analytics-catalog-postgres-0 -o jsonpath='{.spec.resources.requests.storage}')"
        --set-string "superset.storage=$(kubectl -n analytics get pvc data-recsys-analytics-superset-postgres-0 -o jsonpath='{.spec.resources.requests.storage}')"
      )
      ;;
  esac
  if ! helm upgrade "${release}" "${ROOT}/${chart}" -n "${namespace}" \
    --reset-values "${helm_args[@]}" --wait=hookOnly; then
    rm -f "${current_values}"
    return 1
  fi
  rm -f "${current_values}"
}

suspend_excluded() {
  helm_profile recsys-data-lakehouse recsys-dataflow infra/helm/recsys-data-lakehouse
  helm_profile recsys-source-store recsys-dataflow infra/helm/recsys-source-store
  helm_profile recsys-event-stream recsys-dataflow infra/helm/recsys-event-stream
  helm_profile recsys-kafka-connect recsys-dataflow infra/helm/recsys-kafka-connect
  helm_profile recsys-streaming recsys-dataflow infra/helm/recsys-streaming
  helm_profile recsys-airflow recsys-dataflow infra/helm/recsys-airflow
  helm_profile recsys-analytics analytics infra/helm/recsys-analytics
  helm_profile recsys-demo-web api-serving infra/helm/recsys-demo-web
  kubectl -n datahub scale deployment,statefulset --all --replicas=0 || true
}

apply_retained_overlays() {
  helm_profile recsys-observability observability infra/helm/recsys-observability
  helm_profile recsys-llm-serving llm-inference infra/helm/recsys-llm-serving
  helm_profile recsys-serving kserve-triton-inference infra/helm/recsys-serving
  helm_profile recsys-online-feature-api api-serving infra/helm/recsys-online-feature-api
  helm_profile recsys-inference-api api-serving infra/helm/recsys-inference-api
  helm_profile recsys-rag-api api-serving infra/helm/recsys-rag-api
  helm_profile recsys-feature-rag-mcp kagent infra/helm/recsys-feature-rag-mcp
  helm_profile recsys-kagent-agent kagent infra/helm/recsys-kagent-agent
  helm_profile recsys-recommendation-mcp kagent infra/helm/recsys-recommendation-mcp
  helm_profile recsys-recommendation-agent kagent infra/helm/recsys-recommendation-agent
  helm_profile recsys-coordinator-agent kagent infra/helm/recsys-coordinator-agent
  kubectl -n kagent scale deployment -l app.kubernetes.io/component=controller --replicas=1 || true
}

patch_workerpool_strategy() {
  local deployment count=0
  for deployment in $(kubectl -n kagent get deployment -o json | jq -r '.items[] | select(.metadata.labels["ate.dev/worker-pool"] or .metadata.labels["kagent.dev/worker-pool"]) | .metadata.name'); do
    kubectl -n kagent patch "deployment/${deployment}" --type merge -p '{"spec":{"strategy":{"type":"Recreate","rollingUpdate":null}}}'
    count=$((count + 1))
  done
  [[ "${count}" == "3" ]] || {
    echo "expected three WorkerPool-generated Deployments, patched ${count}" >&2
    return 1
  }
  kubectl -n kagent get deployment -o json | jq -e '[.items[] | select(.metadata.labels["ate.dev/worker-pool"] or .metadata.labels["kagent.dev/worker-pool"]) | select(.spec.strategy.type == "Recreate")] | length == 3' >/dev/null
}

materialize_online() {
  local image
  image="$(kubectl -n recsys-dataflow get configmap recsys-data-platform-config -o jsonpath='{.data.FEATURE_STORE_IMAGE}')"
  [[ -n "${image}" ]] || { echo "FEATURE_STORE_IMAGE missing" >&2; return 1; }
  kubectl -n recsys-dataflow delete job compact-feature-materialize --ignore-not-found
  kubectl -n recsys-dataflow create job compact-feature-materialize --image="${image}" \
    --dry-run=client -o json -- bash -lc \
    'export FEAST_SQL_REGISTRY_URL="$(python -m recsys_feature_store_runtime.sql_registry_state url)"; python -m feature_store.materialize_online' \
    | jq '.spec.template.spec.containers[0].envFrom = [{"configMapRef":{"name":"recsys-data-platform-config"}},{"secretRef":{"name":"recsys-data-platform-secret"}}]' \
    | kubectl apply -f -
  kubectl -n recsys-dataflow wait --for=condition=complete job/compact-feature-materialize --timeout=20m
  kubectl -n recsys-dataflow logs job/compact-feature-materialize
}

trigger_jenkins() {
  if [[ -z "${JENKINS_URL:-}" || -z "${JENKINS_USER:-}" || -z "${JENKINS_TOKEN:-}" ]]; then
    echo "Jenkins credentials not exported; trigger with the compact parameters after push."
    return 0
  fi
  local output queue_url build_url payload result
  output="$(FORCE_COMPONENTS="${COMPACT_COMPONENTS}" FORCE_COMPONENTS_MODE=replace \
    CAPACITY_PROFILE=compact-12vcpu PUBLISH_IMAGES=true DEPLOY_PULL_REQUESTS=true \
    FORCE_DEPLOY=true COMPONENT_CI_MAX_PARALLEL=1 \
    "${ROOT}/ops/gcp/trigger_full_jenkins.sh")"
  echo "${output}"
  queue_url="$(sed -n 's/^Full Jenkins CI\/CD queued: //p' <<<"${output}")"
  [[ -n "${queue_url}" ]] || { echo "Jenkins queue URL missing" >&2; return 1; }
  for _ in $(seq 1 120); do
    payload="$(curl -fsS --user "${JENKINS_USER}:${JENKINS_TOKEN}" "${queue_url%/}/api/json")"
    build_url="$(jq -r '.executable.url // empty' <<<"${payload}")"
    [[ -n "${build_url}" ]] && break
    [[ "$(jq -r '.cancelled // false' <<<"${payload}")" == "true" ]] && return 1
    sleep 5
  done
  [[ -n "${build_url}" ]] || { echo "Jenkins build did not leave the queue" >&2; return 1; }
  for _ in $(seq 1 360); do
    payload="$(curl -fsS --user "${JENKINS_USER}:${JENKINS_TOKEN}" "${build_url%/}/api/json")"
    if [[ "$(jq -r '.building' <<<"${payload}")" == "false" ]]; then
      result="$(jq -r '.result' <<<"${payload}")"
      [[ "${result}" == "SUCCESS" ]] || { echo "Jenkins result: ${result}" >&2; return 1; }
      curl -fsS --user "${JENKINS_USER}:${JENKINS_TOKEN}" "${build_url%/}/testReport/api/json" >/dev/null
      [[ "$(jq '.artifacts | length' <<<"${payload}")" -gt 0 ]] || { echo "Jenkins build has no archived artifacts" >&2; return 1; }
      echo "Jenkins compact build SUCCESS: ${build_url}"
      return 0
    fi
    sleep 10
  done
  echo "timed out waiting for Jenkins compact build" >&2
  return 1
}

verify_pvcs() {
  local current="${STATE_DIR}/pvc-identities.current.json"
  local baseline_copy="${STATE_DIR}/pvc-identities.before-up.json"
  record_pvcs "${current}"
  [[ -f "${baseline_copy}" ]] || { echo "PVC baseline is missing" >&2; return 1; }
  if ! cmp -s "${baseline_copy}" "${current}"; then
    diff -u "${baseline_copy}" "${current}" || true
    return 1
  fi
  echo "PVC identity check OK: UID, PV and volume handle are unchanged"
}

verify_cpu_headroom() {
  kubectl get nodes -o json >"${STATE_DIR}/nodes.current.json"
  kubectl get pods -A -o json >"${STATE_DIR}/pods.current.json"
  python3 - "${STATE_DIR}/nodes.current.json" "${STATE_DIR}/pods.current.json" <<'PY'
import json, re, sys

def millicpu(value: str) -> int:
    if value.endswith("m"):
        return int(value[:-1])
    if value.endswith("n"):
        return int(value[:-1]) // 1_000_000
    if value.endswith("u"):
        return int(value[:-1]) // 1_000
    return int(float(value) * 1000)

nodes = json.load(open(sys.argv[1], encoding="utf-8"))["items"]
pods = json.load(open(sys.argv[2], encoding="utf-8"))["items"]
allocatable = sum(millicpu(node["status"]["allocatable"]["cpu"]) for node in nodes)
requested = 0
for pod in pods:
    if not pod.get("spec", {}).get("nodeName") or pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
        continue
    containers = sum(millicpu(container.get("resources", {}).get("requests", {}).get("cpu", "0")) for container in pod["spec"].get("containers", []))
    init = max((millicpu(container.get("resources", {}).get("requests", {}).get("cpu", "0")) for container in pod["spec"].get("initContainers", [])), default=0)
    requested += max(containers, init) + millicpu(pod["spec"].get("overhead", {}).get("cpu", "0"))
headroom = allocatable - requested
print(f"CPU requests: allocatable={allocatable}m requested={requested}m headroom={headroom}m")
if headroom < 750:
    raise SystemExit("compact profile has less than 750m allocatable CPU headroom")
PY
}

verify() {
  get_credentials
  local nodes pending cpu_type ml_type
  nodes="$(kubectl get nodes --no-headers | awk '$2=="Ready" {count++} END {print count+0}')"
  [[ "${nodes}" == "2" ]] || { echo "expected exactly two Ready nodes, got ${nodes}" >&2; return 1; }
  cpu_type="$(gcloud container node-pools describe "${CPU_POOL}" --cluster "${CLUSTER}" --zone "${ZONE}" --project "${PROJECT_ID}" --format='value(config.machineType)')"
  ml_type="$(gcloud container node-pools describe "${ML_POOL}" --cluster "${CLUSTER}" --zone "${ZONE}" --project "${PROJECT_ID}" --format='value(config.machineType)')"
  [[ "${cpu_type}" == "n2-standard-8" && "${ml_type}" == "e2-standard-4" ]]
  pending="$(kubectl get pods -A --field-selector=status.phase=Pending --no-headers 2>/dev/null | wc -l | tr -d ' ')"
  [[ "${pending}" == "0" ]] || { kubectl get pods -A --field-selector=status.phase=Pending; return 1; }
  verify_pvcs
  verify_cpu_headroom
  for target in \
    observability/deployment/recsys-prometheus \
    observability/deployment/recsys-grafana \
    api-serving/deployment/recsys-rag-api \
    api-serving/deployment/recsys-online-feature-api; do
    IFS=/ read -r namespace kind name <<<"${target}"
    kubectl -n "${namespace}" rollout status "${kind}/${name}" --timeout=10m
  done
  patch_workerpool_strategy
  echo "compact verification OK: N2=8, E2=4, global requested capacity=12"
}

rollback_compact_nodes_to_zero() {
  local pool
  echo "compact rollout failed; returning compact node pools to zero and keeping snapshots/PVCs" >&2
  for pool in "${CPU_POOL}" "${ML_POOL}"; do
    gcloud container node-pools update "${pool}" --cluster "${CLUSTER}" \
      --zone "${ZONE}" --project "${PROJECT_ID}" --enable-autoscaling \
      --min-nodes=0 --max-nodes=1 --quiet || true
    gcloud container clusters resize "${CLUSTER}" --node-pool "${pool}" \
      --num-nodes=0 --zone "${ZONE}" --project "${PROJECT_ID}" --quiet || true
  done
}

up() {
  require_compact_branch
  preflight
  get_credentials
  ensure_snapshot
  trap 'status=$?; if [[ ${status} -ne 0 ]]; then rollback_compact_nodes_to_zero; fi; exit ${status}' EXIT
  cp "${PVC_BASELINE}" "${STATE_DIR}/pvc-identities.before-up.json"
  suspend_excluded
  terraform_plan
  terraform -chdir="${TF_DIR}" apply -input=false -auto-approve "${PLAN_FILE}"
  get_credentials
  kubectl wait --for=condition=Ready nodes --all --timeout=20m
  apply_retained_overlays
  patch_workerpool_strategy
  materialize_online
  trigger_jenkins
  verify
  trap - EXIT
}

down() {
  require_compact_branch
  get_credentials
  record_pvcs
  for pool in "${CPU_POOL}" "${ML_POOL}"; do
    gcloud container node-pools update "${pool}" --cluster "${CLUSTER}" \
      --zone "${ZONE}" --project "${PROJECT_ID}" --enable-autoscaling \
      --min-nodes=0 --max-nodes=1 --quiet
    gcloud container clusters resize "${CLUSTER}" --node-pool "${pool}" \
      --num-nodes=0 --zone "${ZONE}" --project "${PROJECT_ID}" --quiet
  done
}

restore_standard() {
  local global_limit e2_limit main_path
  global_limit="$(gcloud compute project-info describe --project "${PROJECT_ID}" --format=json | jq -r '.quotas[] | select(.metric=="CPUS_ALL_REGIONS") | .limit')"
  e2_limit="$(gcloud compute regions describe "${REGION}" --project "${PROJECT_ID}" --format=json | jq -r '.quotas[] | select(.metric=="E2_CPUS") | .limit')"
  python3 - "${global_limit}" "${e2_limit}" <<'PY'
import sys
if float(sys.argv[1]) < 20 or float(sys.argv[2]) < 20:
    raise SystemExit("restore-standard requires at least 20 global and E2 CPUs")
PY
  main_path="$(git -C "${ROOT}" worktree list --porcelain | awk '/^worktree / {p=substr($0,10)} /^branch refs\/heads\/main$/ {print p}')"
  [[ -n "${main_path}" ]] || { echo "main worktree not found" >&2; exit 2; }
  COMPACT_BASE_TFVARS="${main_path}/infra/terraform/gcp/terraform.tfvars" \
    terraform -chdir="${main_path}/infra/terraform/gcp" apply -input=false -auto-approve
  echo "standard infrastructure restored from ${main_path}; run its normal Helm/Jenkins deployment to resume suspended services"
}

require_tools
case "${ACTION}" in
  plan) require_compact_branch; preflight; get_credentials; record_pvcs; terraform_plan ;;
  snapshot) require_compact_branch; snapshot ;;
  up) up ;;
  verify) verify ;;
  down) down ;;
  restore-standard) restore_standard ;;
  help|-h|--help|"") usage ;;
  *) usage >&2; exit 2 ;;
esac
