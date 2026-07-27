#!/usr/bin/env bash

deploy_demo_security_unlocked() {
  helm_atomic_upgrade recsys-security infra/helm/recsys-security \
    recsys-security "${timeout}" --reuse-values
}

migrate_demo_ingress_split() {
  local legacy_api_path=""
  local frontend_upstream="recsys-demo-web.${namespace_demo}.svc.cluster.local"
  legacy_api_path="$(kubectl get ingress/recsys-demo-web -n "${namespace_demo}" \
    -o jsonpath='{range .spec.rules[*].http.paths[*]}{.path}{"\n"}{end}' 2>/dev/null \
    | grep -Fx '/api' || true)"
  if [[ -z "${legacy_api_path}" ]] || kubectl get ingress/recsys-demo-api \
    -n "${namespace_demo}" >/dev/null 2>&1; then
    return 0
  fi
  tx_snapshot_k8s_resource ingress recsys-demo-web "${namespace_demo}"
  kubectl patch ingress/recsys-demo-web -n "${namespace_demo}" --type=json \
    --patch "[\
      {\"op\":\"add\",\"path\":\"/metadata/annotations/nginx.ingress.kubernetes.io~1upstream-vhost\",\"value\":\"${frontend_upstream}\"},\
      {\"op\":\"replace\",\"path\":\"/spec/rules/0/http/paths\",\"value\":[{\"path\":\"/\",\"pathType\":\"Prefix\",\"backend\":{\"service\":{\"name\":\"recsys-demo-web\",\"port\":{\"number\":80}}}}]}\
    ]"
}

deploy_demo_web_unlocked() {
  with_file_lock "/tmp/recsys-security-helm.lock" deploy_demo_security_unlocked
  migrate_demo_ingress_split
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
