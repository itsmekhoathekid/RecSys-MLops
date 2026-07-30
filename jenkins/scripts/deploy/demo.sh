#!/usr/bin/env bash

deploy_demo_web_unlocked() {
  helm_atomic_upgrade "${DEMO_WEB_RELEASE:-recsys-demo-web}" infra/helm/recsys-demo-web \
    "${namespace_demo}" "${timeout}" \
    -f infra/helm/recsys-demo-web/values-gcp.yaml \
    --set "frontend.image=$(image recsys-demo-web)" \
    --set "backend.image=$(image recsys-demo-api)"
  verify_and_wait_workload deployment recsys-demo-web "${namespace_demo}" "$(image recsys-demo-web)"
  verify_and_wait_workload deployment recsys-demo-api "${namespace_demo}" "$(image recsys-demo-api)"
  kubectl wait --for=condition=Ready externalsecret/recsys-demo-web-db \
    -n "${namespace_demo}" --timeout="${timeout}"
  kubectl wait --for=condition=Ready certificate/recsys-web-tls \
    -n "${namespace_demo}" --timeout="${timeout}"
  kubectl wait --for=jsonpath='{.status.loadBalancer.ingress[0].ip}' ingress/recsys-demo-web \
    -n "${namespace_demo}" --timeout="${timeout}"
}

deploy_demo_web() {
  with_file_lock "/tmp/recsys-demo-web-helm.lock" deploy_demo_web_unlocked
}
