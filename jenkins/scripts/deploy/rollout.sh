#!/usr/bin/env bash

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
