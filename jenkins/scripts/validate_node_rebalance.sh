#!/usr/bin/env bash
set -euo pipefail

ML_NODE_SELECTOR_KEY="${ML_NODE_SELECTOR_KEY:-recsys.ai/pool}"
ML_NODE_SELECTOR_VALUE="${ML_NODE_SELECTOR_VALUE:-ml-system}"
CPU_NODE_SELECTOR_KEY="${CPU_NODE_SELECTOR_KEY:-recsys.ai/pool}"
CPU_NODE_SELECTOR_VALUE="${CPU_NODE_SELECTOR_VALUE:-cpu-services}"

section() {
  printf '\n== %s ==\n' "$1"
}

resource_exists() {
  local kind="$1"
  local namespace="$2"
  local name="$3"
  kubectl get "${kind}/${name}" -n "${namespace}" >/dev/null 2>&1
}

assert_deployment_selector() {
  local namespace="$1"
  local deployment="$2"
  local key="$3"
  local expected="$4"

  if ! resource_exists deployment "${namespace}" "${deployment}"; then
    echo "Skipping ${namespace}/deployment/${deployment}; not installed in this environment."
    return 0
  fi

  local actual
  actual="$(kubectl get deployment "${deployment}" -n "${namespace}" -o "go-template={{index .spec.template.spec.nodeSelector \"${key}\"}}")"
  echo "${namespace}/deployment/${deployment} ${key}=${actual}"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "Expected ${namespace}/deployment/${deployment} ${key}=${expected}, got ${actual}." >&2
    exit 1
  fi
}

assert_no_istio_sidecar() {
  local kind="$1"
  local namespace="$2"
  local name="$3"

  if ! resource_exists "${kind}" "${namespace}" "${name}"; then
    echo "Skipping sidecar check for ${namespace}/${kind}/${name}; not installed."
    return 0
  fi

  local inject
  inject="$(kubectl get "${kind}/${name}" -n "${namespace}" -o 'go-template={{index .spec.template.metadata.annotations "sidecar.istio.io/inject"}}')"
  echo "${namespace}/${kind}/${name} sidecar.istio.io/inject=${inject}"
  if [[ "${inject}" != "false" ]]; then
    echo "Expected ${namespace}/${kind}/${name} to disable Istio sidecar injection." >&2
    exit 1
  fi
}

assert_istio_sidecar_enabled() {
  local kind="$1"
  local namespace="$2"
  local name="$3"

  if ! resource_exists "${kind}" "${namespace}" "${name}"; then
    echo "Skipping sidecar enabled check for ${namespace}/${kind}/${name}; not installed."
    return 0
  fi

  local inject
  inject="$(kubectl get "${kind}/${name}" -n "${namespace}" -o 'go-template={{index .spec.template.metadata.annotations "sidecar.istio.io/inject"}}')"
  echo "${namespace}/${kind}/${name} sidecar.istio.io/inject=${inject}"
  if [[ "${inject}" != "true" ]]; then
    echo "Expected ${namespace}/${kind}/${name} to enable Istio sidecar injection for mTLS upstreams." >&2
    exit 1
  fi
}

assert_no_bad_pods() {
  local bad
  bad="$(kubectl get pods -A --no-headers | awk '$4 ~ /Pending|Failed|CrashLoopBackOff|ImagePullBackOff|ErrImagePull|ContainerStatusUnknown|Error/ {print}')"
  if [[ -n "${bad}" ]]; then
    echo "Pods with bad states:" >&2
    printf '%s\n' "${bad}" >&2
    exit 1
  fi
}

assert_no_local_images() {
  local bad
  bad="$(kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"/"}{.metadata.name}{" "}{range .spec.containers[*]}{.image}{" "}{end}{"\n"}{end}' | rg ':local($|[[:space:]])' || true)"
  if [[ -n "${bad}" ]]; then
    echo "Pods still using :local images:" >&2
    printf '%s\n' "${bad}" >&2
    exit 1
  fi
}

section "Cluster Pod Health"
assert_no_bad_pods
assert_no_local_images

section "ML Node Deployments"
for item in \
  "kubeflow ml-pipeline" \
  "kubeflow ml-pipeline-ui" \
  "kubeflow workflow-controller" \
  "kubeflow kuberay-operator" \
  "kserve kserve-controller-manager" \
  "kserve kserve-localmodel-controller-manager" \
  "ingress-nginx ingress-nginx-controller" \
  "istio-system istiod" \
  "experiment-tracking minio" \
  "experiment-tracking mlflow" \
  "experiment-tracking postgres" \
  "kube-system event-exporter-gke" \
  "kube-system konnectivity-agent" \
  "kube-system konnectivity-agent-autoscaler" \
  "kube-system kube-dns-autoscaler" \
  "kube-system l7-default-backend" \
  "kube-system metrics-server-v1.35.1"; do
  read -r namespace deployment <<<"${item}"
  assert_deployment_selector "${namespace}" "${deployment}" "${ML_NODE_SELECTOR_KEY}" "${ML_NODE_SELECTOR_VALUE}"
done

section "CPU Node Exceptions"
assert_deployment_selector kube-system kube-dns "${CPU_NODE_SELECTOR_KEY}" "${CPU_NODE_SELECTOR_VALUE}"
assert_deployment_selector observability recsys-prometheus "${CPU_NODE_SELECTOR_KEY}" "${CPU_NODE_SELECTOR_VALUE}"
assert_deployment_selector ci recsys-jenkins "${CPU_NODE_SELECTOR_KEY}" "${CPU_NODE_SELECTOR_VALUE}"
assert_deployment_selector ci recsys-registry "${CPU_NODE_SELECTOR_KEY}" "${CPU_NODE_SELECTOR_VALUE}"

section "Sidecar Resource Guard"
assert_istio_sidecar_enabled deployment ingress-nginx ingress-nginx-controller
assert_no_istio_sidecar daemonset observability recsys-promtail

echo "Node rebalance validation passed."
