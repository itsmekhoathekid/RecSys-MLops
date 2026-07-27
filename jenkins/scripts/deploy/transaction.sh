#!/usr/bin/env bash

TX_ACTIVE=0
TX_ROLLING_BACK=0
TX_ID=""
TX_DIR=""
TX_JOURNAL=""
TX_COMPONENT=""
TX_STATE_ROOT=""
TX_RECOVERING=0
TX_LOCK_FDS=()

tx_python() {
  python3 -m jenkins.python.deploy_transaction.journal "$@"
}

tx_runtime_python() {
  if [[ -n "${UV_PROJECT_ENVIRONMENT:-}" && -x "${UV_PROJECT_ENVIRONMENT}/bin/python" ]]; then
    "${UV_PROJECT_ENVIRONMENT}/bin/python" "$@"
  else
    python3 "$@"
  fi
}

tx_state_root() {
  if [[ -n "${DEPLOY_STATE_ROOT:-}" ]]; then
    printf '%s' "${DEPLOY_STATE_ROOT}"
  elif [[ -n "${JENKINS_HOME:-}" ]]; then
    printf '%s' "${JENKINS_HOME}/ci-transactions"
  else
    printf '%s' ".ci-transactions"
  fi
}

tx_component_lock_names() {
  case "$1" in
    materialize|spark_batch|dp1|dp2|dp3|stream_offline|stream_online|drift)
      printf '%s\n' recsys-data-platform
      ;;
    training)
      printf '%s\n' recsys-data-platform recsys-mlflow
      ;;
    api|kserve|kserve_model_cd)
      printf '%s\n' recsys-serving
      ;;
    rollout)
      printf '%s\n' jenkins-jobs recsys-rollout-watcher
      ;;
    analytics)
      printf '%s\n' recsys-analytics recsys-data-platform
      ;;
    demo_web)
      printf '%s\n' recsys-demo-web recsys-security
      ;;
    mlflow)
      printf '%s\n' recsys-mlflow
      ;;
    all)
      printf '%s\n' \
        jenkins-jobs recsys-analytics recsys-data-platform recsys-demo-web \
        recsys-mlflow recsys-rollout-watcher recsys-security recsys-serving
      ;;
    *)
      printf '%s\n' "$1"
      ;;
  esac | LC_ALL=C sort -u
}

tx_acquire_component_locks() {
  local component="$1"
  local lock_root="${DEPLOY_LOCK_ROOT:-${JENKINS_HOME:-.ci-locks}/ci-locks}"
  local lock_name lock_path lock_fd
  mkdir -p "${lock_root}"
  if ! command -v flock >/dev/null 2>&1; then
    if [[ "${DEPLOY_TARGET:-local}" == "gcp-production" ]]; then
      recsys_error "flock is required for production component transactions"
      return 2
    fi
    recsys_log "flock unavailable; local transaction is not cross-process serialized"
    return 0
  fi
  TX_LOCK_FDS=()
  while IFS= read -r lock_name; do
    [[ -n "${lock_name}" ]] || continue
    lock_path="${lock_root}/$(recsys_slug "${lock_name}").lock"
    exec {lock_fd}>"${lock_path}"
    if ! flock -w "${DEPLOY_LOCK_TIMEOUT_SECONDS:-1800}" "${lock_fd}"; then
      recsys_error "timed out acquiring deployment lock ${lock_name}"
      eval "exec ${lock_fd}>&-"
      tx_release_component_locks
      return 4
    fi
    TX_LOCK_FDS+=("${lock_fd}")
  done < <(tx_component_lock_names "${component}")
}

tx_release_component_locks() {
  local lock_fd
  for lock_fd in "${TX_LOCK_FDS[@]:-}"; do
    flock -u "${lock_fd}" >/dev/null 2>&1 || true
    eval "exec ${lock_fd}>&-"
  done
  TX_LOCK_FDS=()
}

tx_archive_journal() {
  local destination="reports/gcp/${TX_COMPONENT:-unknown}"
  [[ -n "${TX_JOURNAL:-}" && -f "${TX_JOURNAL}" ]] || return 0
  mkdir -p "${destination}"
  cp "${TX_JOURNAL}" "${destination}/transaction.json"
}

tx_recover_component() {
  local component="$1"
  local state_root="$2"
  local blocking_paths=""
  local journal state
  blocking_paths="$(tx_python blocking --root "${state_root}" --component "${component}" 2>/dev/null || true)"
  while IFS= read -r journal; do
    [[ -n "${journal}" ]] || continue
    state="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state"])' "${journal}")"
    if [[ "${state}" == "ROLLBACK_FAILED" ]]; then
      recsys_error "transaction is ROLLBACK_FAILED and requires operator repair: ${journal}"
      return 3
    fi
    TX_JOURNAL="${journal}"
    TX_DIR="$(dirname "${journal}")"
    TX_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["transactionId"])' "${journal}")"
    TX_COMPONENT="${component}"
    export TX_ID TX_DIR TX_JOURNAL TX_COMPONENT
    if [[ "${state}" == "PREFLIGHT" || "${state}" == "SNAPSHOT" ]]; then
      tx_python state \
        --path "${TX_JOURNAL}" \
        --state ROLLED_BACK \
        --message "crash recovery found no APPLYING mutation"
      recsys_log "closed pre-apply transaction ${TX_ID}"
      continue
    fi
    TX_ACTIVE=1
    TX_RECOVERING=1
    TX_ROLLING_BACK=0
    tx_abort "crash recovery before a new ${component} deployment" || return 3
    TX_RECOVERING=0
  done <<<"${blocking_paths}"
  TX_ACTIVE=0
  TX_ROLLING_BACK=0
}

tx_begin() {
  local component="$1"
  local git_sha="${IMAGE_TAG:-${GIT_COMMIT:-unknown}}"
  local job_slug
  local build_slug
  TX_COMPONENT="${component}"
  TX_STATE_ROOT="$(tx_state_root)"
  mkdir -p "${TX_STATE_ROOT}"
  tx_acquire_component_locks "${component}"
  tx_recover_component "${component}" "${TX_STATE_ROOT}"
  job_slug="$(recsys_slug "${JOB_NAME:-local}")"
  build_slug="$(recsys_slug "${BUILD_NUMBER:-manual}")"
  TX_ID="${job_slug}-${build_slug}-$(recsys_slug "${component}")-$(recsys_slug "${git_sha}")"
  TX_DIR="${TX_STATE_ROOT}/${TX_ID}"
  TX_JOURNAL="${TX_DIR}/transaction.json"
  mkdir -p "${TX_DIR}"

  if ! tx_python blocking --root "${TX_STATE_ROOT}" --component "${component}"; then
    recsys_error "component ${component} has an unfinished or failed deployment transaction"
    return 3
  fi

  tx_python init \
    --path "${TX_JOURNAL}" \
    --transaction-id "${TX_ID}" \
    --component "${component}" \
    --git-sha "${git_sha}"
  TX_ACTIVE=1
  export TX_ID TX_DIR TX_JOURNAL TX_COMPONENT
  recsys_log "started deployment transaction ${TX_ID}"
}

tx_transition() {
  local state="$1"
  local message="${2:-}"
  tx_python state --path "${TX_JOURNAL}" --state "${state}" --message "${message}"
}

tx_snapshot_helm_release() {
  local release="$1"
  local namespace="$2"
  local revision=""
  local workload_snapshot_path=""
  local existed=0
  if helm status "${release}" -n "${namespace}" >/dev/null 2>&1; then
    existed=1
    revision="$(helm_current_revision "${release}" "${namespace}")"
    [[ -n "${revision}" ]] || {
      recsys_error "cannot resolve deployed revision for ${release} in ${namespace}"
      return 1
    }
    helm get values "${release}" -n "${namespace}" -a -o yaml \
      >"${TX_DIR}/helm-${namespace}-${release}-values.yaml"
    workload_snapshot_path="${TX_DIR}/workloads-${namespace}-${release}.json"
    tx_capture_workload_images "${namespace}" "${workload_snapshot_path}"
  fi
  tx_python add-release \
    --path "${TX_JOURNAL}" \
    --release "${release}" \
    --namespace "${namespace}" \
    --existed "${existed}" \
    --revision "${revision}" \
    --workload-snapshot-path "${workload_snapshot_path}"
}

tx_capture_workload_images() {
  local namespace="$1"
  local output_path="$2"
  kubectl get deployment,statefulset,daemonset -n "${namespace}" -o json \
    | python3 -c '
import json, sys
from pathlib import Path
payload = json.load(sys.stdin)
snapshot = {}
for item in payload.get("items", []):
    identity = f"{item['"'"'kind'"'"']}/{item['"'"'metadata'"'"']['"'"'name'"'"']}"
    snapshot[identity] = sorted(
        container["image"]
        for container in item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    )
Path(sys.argv[1]).write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
' "${output_path}"
}

tx_verify_workload_images() {
  local namespace="$1"
  local expected_path="$2"
  local actual_path="${expected_path}.actual"
  local identity
  [[ -s "${expected_path}" ]] || return 0
  tx_capture_workload_images "${namespace}" "${actual_path}"
  python3 - "${expected_path}" "${actual_path}" <<'PY'
import json
import sys

expected = json.load(open(sys.argv[1], encoding="utf-8"))
actual = json.load(open(sys.argv[2], encoding="utf-8"))
for identity, images in expected.items():
    if actual.get(identity) != images:
        raise SystemExit(
            f"rollback image mismatch for {identity}: expected={images}, actual={actual.get(identity)}"
        )
PY
  while IFS= read -r identity; do
    [[ -n "${identity}" ]] || continue
    kubectl rollout status "${identity}" -n "${namespace}" \
      --timeout="${COMPONENT_DEPLOY_TIMEOUT:-600s}"
  done < <(python3 -c 'import json,sys; [print(x) for x in json.load(open(sys.argv[1]))]' "${expected_path}")
  rm -f "${actual_path}"
}

tx_register_external() {
  local kind="$1"
  local state_path="$2"
  tx_python add-external \
    --path "${TX_JOURNAL}" \
    --kind "${kind}" \
    --state-path "${state_path}"
}

tx_record_health_test() {
  tx_python add-test \
    --path "${TX_JOURNAL}" \
    --profile "$1" \
    --status "$2" \
    --report-path "$3"
}

tx_snapshot_k8s_resource() {
  local kind="$1"
  local name="$2"
  local namespace="$3"
  local slug
  local state_path
  local yaml_path
  local existed=0
  [[ "${TX_ACTIVE}" == "1" ]] || return 0
  slug="$(recsys_slug "${kind}-${namespace}-${name}")"
  state_path="${TX_DIR}/k8s-${slug}.json"
  yaml_path="${TX_DIR}/k8s-${slug}.yaml"
  if kubectl get "${kind}/${name}" -n "${namespace}" -o yaml >"${yaml_path}" 2>/dev/null; then
    existed=1
  else
    : >"${yaml_path}"
  fi
  python3 - "${state_path}" "${yaml_path}" "${kind}" "${name}" "${namespace}" "${existed}" <<'PY'
import json
import sys
from pathlib import Path

state_path, yaml_path, kind, name, namespace, existed = sys.argv[1:]
Path(state_path).write_text(
    json.dumps(
        {
            "yamlPath": yaml_path,
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "existed": existed == "1",
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
  tx_register_external k8s-resource "${state_path}"
}

tx_restore_k8s_resource() {
  local state_path="$1"
  local kind name namespace existed yaml_path
  kind="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["kind"])' "${state_path}")"
  name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["name"])' "${state_path}")"
  namespace="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["namespace"])' "${state_path}")"
  existed="$(python3 -c 'import json,sys; print("1" if json.load(open(sys.argv[1]))["existed"] else "0")' "${state_path}")"
  yaml_path="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["yamlPath"])' "${state_path}")"
  if [[ "${existed}" == "1" ]]; then
    kubectl apply -f "${yaml_path}"
    kubectl get "${kind}/${name}" -n "${namespace}" >/dev/null
  else
    kubectl delete "${kind}/${name}" -n "${namespace}" --ignore-not-found --wait=true
  fi
}

tx_rollback_external_record() {
  local record="$1"
  local kind state_path
  kind="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["kind"])' "${record}")"
  state_path="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["statePath"])' "${record}")"
  case "${kind}" in
    kfp-version)
      tx_runtime_python apps/ml-system/src/kubeflow/delete_pipeline_version.py \
        --state-path "${state_path}"
      ;;
    model-store)
      tx_runtime_python -m jenkins.python.model_cd.storage restore --state-path "${state_path}"
      ;;
    jenkins-jobs)
      jenkins_restore_job_configs "${state_path}"
      ;;
    k8s-resource)
      tx_restore_k8s_resource "${state_path}"
      ;;
    database-migration)
      database_rollback_migration "${state_path}"
      ;;
    airflow-database-migration)
      database_rollback_airflow_migration "${state_path}"
      ;;
    *)
      recsys_error "unknown external compensation kind: ${kind}"
      return 2
      ;;
  esac
  tx_python mark-rollback \
    --path "${TX_JOURNAL}" \
    --collection external \
    --identifier "${state_path}" \
    --status restored
}

tx_rollback_helm_record() {
  local record="$1"
  local release namespace existed revision workload_snapshot_path
  release="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["release"])' "${record}")"
  namespace="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["namespace"])' "${record}")"
  existed="$(python3 -c 'import json,sys; print("1" if json.loads(sys.argv[1])["existed"] else "0")' "${record}")"
  revision="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["revision"])' "${record}")"
  workload_snapshot_path="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("workloadSnapshotPath", ""))' "${record}")"

  if [[ "${existed}" == "1" ]]; then
    if ! helm rollback "${release}" "${revision}" \
      -n "${namespace}" \
      --wait \
      --cleanup-on-fail \
      --timeout "${COMPONENT_DEPLOY_TIMEOUT:-600s}"; then
      recsys_log "retrying legacy Helm rollback without hooks for ${release} revision ${revision}"
      helm rollback "${release}" "${revision}" \
        -n "${namespace}" \
        --wait \
        --cleanup-on-fail \
        --no-hooks \
        --timeout "${COMPONENT_DEPLOY_TIMEOUT:-600s}"
    fi
    helm status "${release}" -n "${namespace}" -o json \
      | python3 -c '
import json, sys
payload = json.load(sys.stdin)
status = payload.get("info", {}).get("status", "")
if str(status).lower() != "deployed":
    raise SystemExit(f"release did not return to deployed state: {status}")
'
    tx_verify_workload_images "${namespace}" "${workload_snapshot_path}"
  else
    helm uninstall "${release}" \
      -n "${namespace}" \
      --wait \
      --timeout "${COMPONENT_DEPLOY_TIMEOUT:-600s}" || true
    ! helm status "${release}" -n "${namespace}" >/dev/null 2>&1
  fi
  tx_python mark-rollback \
    --path "${TX_JOURNAL}" \
    --collection helm \
    --identifier "${release}" \
    --status restored
}

tx_abort() {
  local reason="${1:-deployment or production test failed}"
  local status=0
  local record
  if [[ "${TX_ACTIVE}" != "1" || "${TX_ROLLING_BACK}" == "1" ]]; then
    return 0
  fi
  TX_ROLLING_BACK=1
  tx_transition ROLLING_BACK "${reason}"

  while IFS= read -r record; do
    [[ -n "${record}" ]] || continue
    tx_rollback_external_record "${record}" || status=1
  done < <(tx_python list --path "${TX_JOURNAL}" --collection external)

  while IFS= read -r record; do
    [[ -n "${record}" ]] || continue
    tx_rollback_helm_record "${record}" || status=1
  done < <(tx_python list --path "${TX_JOURNAL}" --collection helm)

  if [[ "${status}" == "0" ]] && declare -F component_test_verify_rollback >/dev/null 2>&1; then
    component_test_verify_rollback "${TX_COMPONENT}" || status=1
  fi

  if [[ "${status}" == "0" ]]; then
    tx_transition ROLLED_BACK "${reason}"
    tx_archive_journal
    recsys_log "transaction ${TX_ID} rolled back and verified"
  else
    tx_transition ROLLBACK_FAILED "${reason}"
    tx_archive_journal
    recsys_error "transaction ${TX_ID} rollback failed; component is blocked"
  fi
  TX_ACTIVE=0
  TX_ROLLING_BACK=0
  if [[ "${TX_RECOVERING}" != "1" ]]; then
    tx_release_component_locks
  fi
  return "${status}"
}

tx_commit() {
  tx_transition COMMITTED
  tx_archive_journal
  TX_ACTIVE=0
  tx_release_component_locks
  recsys_log "committed deployment transaction ${TX_ID}"
}

tx_handle_exit() {
  local status="$1"
  if [[ "${status}" != "0" && "${TX_ACTIVE}" == "1" ]]; then
    tx_abort "entrypoint exited with status ${status}" || true
  fi
  return "${status}"
}
