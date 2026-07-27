#!/usr/bin/env bash

jenkins_authenticate() {
  local jenkins_url="$1"
  local namespace="$2"
  local admin_secret="$3"
  local cookie_file="$4"
  local crumb_json
  JENKINS_AUTH_USER="$(kubectl get secret "${admin_secret}" -n "${namespace}" -o 'jsonpath={.data.username}' | base64 -d)"
  JENKINS_AUTH_PASSWORD="$(kubectl get secret "${admin_secret}" -n "${namespace}" -o 'jsonpath={.data.password}' | base64 -d)"
  crumb_json="$(curl -fsS -c "${cookie_file}" -u "${JENKINS_AUTH_USER}:${JENKINS_AUTH_PASSWORD}" "${jenkins_url}/crumbIssuer/api/json")"
  JENKINS_CRUMB_HEADER="$(python3 -c 'import json,sys; p=json.load(sys.stdin); print("{}: {}".format(p["crumbRequestField"], p["crumb"]))' <<<"${crumb_json}")"
}

jenkins_snapshot_job_configs() {
  local state_path="$1"
  local jenkins_url="$2"
  local namespace="$3"
  local admin_secret="$4"
  shift 4
  local state_dir="${state_path%.json}"
  local cookie_file="${state_dir}/cookie"
  local rows_file="${state_dir}/jobs.tsv"
  local job config_path http_code checksum
  mkdir -p "${state_dir}"
  : >"${rows_file}"
  jenkins_authenticate "${jenkins_url}" "${namespace}" "${admin_secret}" "${cookie_file}"
  for job in "$@"; do
    config_path="${state_dir}/$(recsys_slug "${job}").xml"
    http_code="$(curl -sS -o "${config_path}" -w '%{http_code}' \
      -u "${JENKINS_AUTH_USER}:${JENKINS_AUTH_PASSWORD}" \
      "${jenkins_url}/job/${job}/config.xml")"
    if [[ "${http_code}" == "200" ]]; then
      checksum="$(recsys_sha256_file "${config_path}")"
      printf '%s\t1\t%s\t%s\n' "${job}" "${config_path}" "${checksum}" >>"${rows_file}"
    elif [[ "${http_code}" == "404" ]]; then
      rm -f "${config_path}"
      printf '%s\t0\t\t\n' "${job}" >>"${rows_file}"
    else
      recsys_error "cannot snapshot Jenkins job ${job}: HTTP ${http_code}"
      rm -f "${cookie_file}"
      return 1
    fi
  done
  python3 - "${state_path}" "${jenkins_url}" "${namespace}" "${admin_secret}" "${rows_file}" <<'PY'
import json
import sys
from pathlib import Path

state_path, url, namespace, secret, rows_path = sys.argv[1:]
jobs = []
for line in Path(rows_path).read_text(encoding="utf-8").splitlines():
    job, existed, config_path, checksum = line.split("\t")
    jobs.append(
        {
            "name": job,
            "existed": existed == "1",
            "configPath": config_path,
            "checksum": checksum,
        }
    )
Path(state_path).write_text(
    json.dumps(
        {
            "jenkinsUrl": url,
            "namespace": namespace,
            "adminSecret": secret,
            "jobs": jobs,
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
  rm -f "${cookie_file}" "${rows_file}"
}

jenkins_restore_job_configs() {
  local state_path="$1"
  local jenkins_url namespace admin_secret state_dir cookie_file
  local record job existed config_path expected actual
  [[ -s "${state_path}" ]] || return 0
  jenkins_url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["jenkinsUrl"])' "${state_path}")"
  namespace="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["namespace"])' "${state_path}")"
  admin_secret="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["adminSecret"])' "${state_path}")"
  state_dir="${state_path%.json}"
  cookie_file="${state_dir}/restore-cookie"
  jenkins_authenticate "${jenkins_url}" "${namespace}" "${admin_secret}" "${cookie_file}"

  while IFS= read -r record; do
    [[ -n "${record}" ]] || continue
    job="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "${record}")"
    existed="$(python3 -c 'import json,sys; print("1" if json.loads(sys.argv[1])["existed"] else "0")' "${record}")"
    if [[ "${existed}" == "1" ]]; then
      config_path="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["configPath"])' "${record}")"
      expected="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["checksum"])' "${record}")"
      curl -fsS -u "${JENKINS_AUTH_USER}:${JENKINS_AUTH_PASSWORD}" \
        -b "${cookie_file}" -H "${JENKINS_CRUMB_HEADER}" \
        -H 'Content-Type: application/xml' \
        --data-binary "@${config_path}" \
        "${jenkins_url}/job/${job}/config.xml" >/dev/null
      actual="$(curl -fsS -u "${JENKINS_AUTH_USER}:${JENKINS_AUTH_PASSWORD}" \
        "${jenkins_url}/job/${job}/config.xml" | recsys_sha256_stdin)"
      [[ "${actual}" == "${expected}" ]] || {
        recsys_error "restored Jenkins job ${job} checksum mismatch"
        rm -f "${cookie_file}"
        return 1
      }
    else
      curl -fsS -u "${JENKINS_AUTH_USER}:${JENKINS_AUTH_PASSWORD}" \
        -b "${cookie_file}" -H "${JENKINS_CRUMB_HEADER}" \
        -X POST "${jenkins_url}/job/${job}/doDelete" >/dev/null 2>&1 || true
    fi
  done < <(python3 -c 'import json,sys; [print(json.dumps(x)) for x in reversed(json.load(open(sys.argv[1]))["jobs"])]' "${state_path}")
  rm -f "${cookie_file}"
}

reconcile_rollout_jenkins_jobs() {
  local values_file="$1"
  local jenkins_url="${JENKINS_URL:-http://recsys-jenkins.${namespace_ci}.svc.cluster.local:8080}"
  local admin_secret="${JENKINS_ADMIN_SECRET_NAME:-recsys-jenkins-admin}"
  local seed_dir="${JENKINS_HOME:-/tmp}/ci-tmp"
  local seed_script="${seed_dir}/recsys-rollout-seed.groovy"
  local username password crumb_json crumb_header cookie_file
  jenkins_url="${jenkins_url%/}"
  if [[ "${RECONCILE_JENKINS_ROLLOUT_JOBS:-1}" == "0" ]]; then
    recsys_log "skipping Jenkins rollout job reconciliation"
    return 0
  fi
  if [[ "${TX_ACTIVE}" == "1" ]]; then
    local jobs_state="${TX_DIR}/jenkins-rollout-jobs.json"
    jenkins_snapshot_job_configs \
      "${jobs_state}" "${jenkins_url}" "${namespace_ci}" "${admin_secret}" \
      RecSys-Progressive-Rollout-CICD RecSys-KServe-Model-CD
    tx_register_external jenkins-jobs "${jobs_state}"
    tx_snapshot_k8s_resource configmap recsys-jenkins-init "${namespace_ci}"
  fi
  helm template recsys-ci infra/helm/recsys-ci \
    --namespace "${namespace_ci}" \
    -f "${values_file}" \
    --set "namespace.name=${namespace_ci}" \
    --show-only templates/jenkins-init-configmap.yaml \
    | kubectl apply -f -
  mkdir -p "${seed_dir}"
  kubectl get configmap recsys-jenkins-init -n "${namespace_ci}" \
    -o 'jsonpath={.data.zz-seed-cicd-views\.groovy}' >"${seed_script}"
  username="$(kubectl get secret "${admin_secret}" -n "${namespace_ci}" -o 'jsonpath={.data.username}' | base64 -d)"
  password="$(kubectl get secret "${admin_secret}" -n "${namespace_ci}" -o 'jsonpath={.data.password}' | base64 -d)"
  cookie_file="${seed_script}.cookie"
  crumb_json="$(curl -fsS -c "${cookie_file}" -u "${username}:${password}" "${jenkins_url}/crumbIssuer/api/json")"
  crumb_header="$(python3 -c 'import json,sys; p=json.load(sys.stdin); print("{}: {}".format(p["crumbRequestField"], p["crumb"]))' <<<"${crumb_json}")"
  curl -fsS \
    -u "${username}:${password}" \
    -b "${cookie_file}" \
    -H "${crumb_header}" \
    --data-urlencode "script@${seed_script}" \
    "${jenkins_url}/scriptText" >/dev/null
  rm -f "${cookie_file}" "${seed_script}"
  recsys_log "reconciled rollout Jenkins jobs without restarting Jenkins"
}

deploy_rollout_watcher() {
  local watcher_image
  local values_file
  watcher_image="$(image recsys-mlops-training)"
  values_file="${ROLLOUT_CI_VALUES_FILE:-infra/helm/recsys-ci/values-gke.yaml}"
  reconcile_rollout_jenkins_jobs "${values_file}"
  tx_snapshot_k8s_resource deployment recsys-model-rollout-watcher "${namespace_ci}"
  helm template recsys-ci infra/helm/recsys-ci \
    --namespace "${namespace_ci}" \
    -f "${values_file}" \
    --set "namespace.name=${namespace_ci}" \
    --set "modelRolloutWatcher.enabled=true" \
    --set "modelRolloutWatcher.image=${watcher_image}" \
    --set "modelRolloutWatcher.imagePullPolicy=Always" \
    --set "modelRolloutWatcher.autoProgressiveEnabled=true" \
    --set-string 'modelRolloutWatcher.progressiveWeights=10\,25\,50' \
    --show-only templates/model-rollout-watcher.yaml \
    | kubectl apply -f -
  verify_and_wait_workload \
    deployment recsys-model-rollout-watcher "${namespace_ci}" "${watcher_image}"
}
