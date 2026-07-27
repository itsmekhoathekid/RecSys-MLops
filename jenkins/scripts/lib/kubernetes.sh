#!/usr/bin/env bash

kube_resource_exists() {
  local kind="$1"
  local name="$2"
  local namespace="$3"
  kubectl get "${kind}/${name}" -n "${namespace}" >/dev/null 2>&1
}

kube_wait_rollout_if_exists() {
  local kind="$1"
  local name="$2"
  local namespace="$3"
  local timeout="$4"
  if ! kube_resource_exists "${kind}" "${name}" "${namespace}"; then
    recsys_log "skip rollout wait: ${kind}/${name} is not installed in ${namespace}"
    return 0
  fi
  kubectl rollout status "${kind}/${name}" -n "${namespace}" --timeout="${timeout}"
}

kube_verify_workload_image() {
  local kind="$1"
  local name="$2"
  local namespace="$3"
  local expected_image="$4"
  local images
  if ! kube_resource_exists "${kind}" "${name}" "${namespace}"; then
    recsys_log "skip image verification: ${kind}/${name} is not installed in ${namespace}"
    return 0
  fi
  images="$(
    kubectl get "${kind}/${name}" -n "${namespace}" \
      -o jsonpath='{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}'
  )"
  printf '%s\n' "${images}"
  grep -Fq "${expected_image}" <<<"${images}" || {
    recsys_error "expected ${expected_image} on ${kind}/${name}, got: ${images}"
    return 1
  }
}
