#!/usr/bin/env bash
set -euo pipefail

ML_NODE_SELECTOR_KEY="${ML_NODE_SELECTOR_KEY:-recsys.ai/pool}"
ML_NODE_SELECTOR_VALUE="${ML_NODE_SELECTOR_VALUE:-ml-system}"
ML_TOLERATION_KEY="${ML_TOLERATION_KEY:-recsys.ai/workload}"
ML_TOLERATION_VALUE="${ML_TOLERATION_VALUE:-ml-system}"
ML_TOLERATION_EFFECT="${ML_TOLERATION_EFFECT:-NoSchedule}"
CPU_NODE_SELECTOR_KEY="${CPU_NODE_SELECTOR_KEY:-recsys.ai/pool}"
CPU_NODE_SELECTOR_VALUE="${CPU_NODE_SELECTOR_VALUE:-cpu-services}"

ml_tolerations_json() {
  cat <<JSON
[{
  "key": "${ML_TOLERATION_KEY}",
  "operator": "Equal",
  "value": "${ML_TOLERATION_VALUE}",
  "effect": "${ML_TOLERATION_EFFECT}"
}]
JSON
}

patch_deployment_ml() {
  local namespace="$1"
  local deployment="$2"
  local os_selector="${3:-false}"
  local node_selector
  if [[ "${os_selector}" == "true" ]]; then
    node_selector="\"kubernetes.io/os\": \"linux\", \"${ML_NODE_SELECTOR_KEY}\": \"${ML_NODE_SELECTOR_VALUE}\""
  else
    node_selector="\"${ML_NODE_SELECTOR_KEY}\": \"${ML_NODE_SELECTOR_VALUE}\""
  fi
  kubectl patch deployment "${deployment}" -n "${namespace}" --type merge -p "{
    \"spec\": {
      \"template\": {
        \"spec\": {
          \"nodeSelector\": {${node_selector}},
          \"tolerations\": $(ml_tolerations_json)
        }
      }
    }
  }"
}

patch_gke_managed_deployment_ml() {
  local deployment="$1"
  local selector_path="${ML_NODE_SELECTOR_KEY//\//~1}"
  kubectl patch deployment "${deployment}" -n kube-system --type json -p "[
    {\"op\": \"add\", \"path\": \"/spec/template/spec/nodeSelector/${selector_path}\", \"value\": \"${ML_NODE_SELECTOR_VALUE}\"}
  ]"

  local existing_toleration
  existing_toleration="$(kubectl get deployment "${deployment}" -n kube-system -o "go-template={{range .spec.template.spec.tolerations}}{{if and (eq .key \"${ML_TOLERATION_KEY}\") (eq .value \"${ML_TOLERATION_VALUE}\")}}{{.key}}{{end}}{{end}}")"
  if [[ -z "${existing_toleration}" ]]; then
    kubectl patch deployment "${deployment}" -n kube-system --type json -p "[
      {\"op\": \"add\", \"path\": \"/spec/template/spec/tolerations/-\", \"value\": {\"key\": \"${ML_TOLERATION_KEY}\", \"operator\": \"Equal\", \"value\": \"${ML_TOLERATION_VALUE}\", \"effect\": \"${ML_TOLERATION_EFFECT}\"}}
    ]"
  fi
}

patch_gke_managed_deployment_cpu() {
  local deployment="$1"
  local selector_path="${CPU_NODE_SELECTOR_KEY//\//~1}"
  kubectl patch deployment "${deployment}" -n kube-system --type json -p "[
    {\"op\": \"add\", \"path\": \"/spec/template/spec/nodeSelector/${selector_path}\", \"value\": \"${CPU_NODE_SELECTOR_VALUE}\"}
  ]"
}

patch_daemonset_ml() {
  local namespace="$1"
  local daemonset="$2"
  kubectl patch daemonset "${daemonset}" -n "${namespace}" --type merge -p "{
    \"spec\": {
      \"template\": {
        \"spec\": {
          \"nodeSelector\": {\"${ML_NODE_SELECTOR_KEY}\": \"${ML_NODE_SELECTOR_VALUE}\"},
          \"tolerations\": $(ml_tolerations_json)
        }
      }
    }
  }"
}

patch_deployment_cpu() {
  local namespace="$1"
  local deployment="$2"
  kubectl patch deployment "${deployment}" -n "${namespace}" --type merge -p "{
    \"spec\": {
      \"template\": {
        \"spec\": {
          \"nodeSelector\": {\"${CPU_NODE_SELECTOR_KEY}\": \"${CPU_NODE_SELECTOR_VALUE}\"},
          \"tolerations\": []
        }
      }
    }
  }"
}

rollout_deployment() {
  local namespace="$1"
  local deployment="$2"
  kubectl rollout status "deployment/${deployment}" -n "${namespace}" --timeout=240s
}

rollout_daemonset() {
  local namespace="$1"
  local daemonset="$2"
  kubectl rollout status "daemonset/${daemonset}" -n "${namespace}" --timeout=240s
}

disable_sidecar_injection() {
  local kind="$1"
  local namespace="$2"
  local name="$3"
  kubectl patch "${kind}" "${name}" -n "${namespace}" --type merge -p '{
    "spec": {
      "template": {
        "metadata": {
          "annotations": {
            "sidecar.istio.io/inject": "false"
          }
        }
      }
    }
  }'
}

enable_ingress_mesh_upstreams() {
  kubectl label namespace ingress-nginx istio-injection=enabled --overwrite
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
}

kubeflow_deployments=(
  cache-deployer-deployment
  cache-server
  controller-manager
  kuberay-operator
  metadata-envoy-deployment
  metadata-grpc-deployment
  metadata-writer
  ml-pipeline
  ml-pipeline-persistenceagent
  ml-pipeline-scheduledworkflow
  ml-pipeline-ui
  ml-pipeline-viewer-crd
  ml-pipeline-visualizationserver
  mysql
  proxy-agent
  seaweedfs
  workflow-controller
)

for deployment in "${kubeflow_deployments[@]}"; do
  patch_deployment_ml kubeflow "${deployment}"
done

kserve_deployments=(
  kserve-controller-manager
  kserve-localmodel-controller-manager
)
for deployment in "${kserve_deployments[@]}"; do
  patch_deployment_ml kserve "${deployment}"
done

# Jenkins builds are CPU and disk intensive. Keeping CI on the already dense
# ML control-plane node can trigger node DiskPressure and evict the controller
# mid-build, so place the stateful CI services on the CPU services pool.
ci_cpu_deployments=(
  recsys-jenkins
  recsys-registry
)
for deployment in "${ci_cpu_deployments[@]}"; do
  patch_deployment_cpu ci "${deployment}"
done
patch_daemonset_ml ci recsys-registry-node-proxy

cert_manager_deployments=(
  cert-manager
  cert-manager-cainjector
  cert-manager-webhook
)
for deployment in "${cert_manager_deployments[@]}"; do
  patch_deployment_ml cert-manager "${deployment}" true
done

external_secret_deployments=(
  external-secrets
  external-secrets-cert-controller
  external-secrets-webhook
)
for deployment in "${external_secret_deployments[@]}"; do
  patch_deployment_ml external-secrets "${deployment}"
done

experiment_tracking_deployments=(
  minio
  mlflow
  postgres
)
kubectl set resources deployment/minio -n experiment-tracking --requests=cpu=100m,memory=512Mi
kubectl set resources deployment/mlflow -n experiment-tracking --requests=cpu=100m,memory=512Mi
kubectl set resources deployment/postgres -n experiment-tracking --requests=cpu=100m,memory=256Mi
for deployment in "${experiment_tracking_deployments[@]}"; do
  patch_deployment_ml experiment-tracking "${deployment}"
done

keda_deployments=(
  keda-add-ons-http-controller-manager
  keda-add-ons-http-external-scaler
  keda-add-ons-http-interceptor
  keda-admission-webhooks
  keda-operator
  keda-operator-metrics-apiserver
)
kubectl set resources \
  deployment/keda-add-ons-http-controller-manager \
  deployment/keda-add-ons-http-external-scaler \
  deployment/keda-add-ons-http-interceptor \
  -n keda \
  --requests=cpu=25m,memory=20Mi \
  --limits=cpu=500m,memory=64Mi
kubectl set resources \
  deployment/keda-admission-webhooks \
  deployment/keda-operator \
  deployment/keda-operator-metrics-apiserver \
  -n keda \
  --requests=cpu=25m,memory=100Mi
for deployment in "${keda_deployments[@]}"; do
  patch_deployment_ml keda "${deployment}" true
done

kubectl set resources deployment/controller-manager deployment/kuberay-operator -n kubeflow --requests=cpu=50m,memory=256Mi --limits=cpu=100m,memory=512Mi
kubectl set resources deployment/ingress-nginx-controller -n ingress-nginx -c controller --requests=cpu=50m,memory=90Mi
enable_ingress_mesh_upstreams
patch_deployment_ml ingress-nginx ingress-nginx-controller true
kubectl set resources deployment/istiod -n istio-system --requests=cpu=100m,memory=512Mi --limits=cpu=1,memory=1Gi
kubectl patch deployment istiod -n istio-system --type merge -p "{
  \"spec\": {
    \"template\": {
      \"spec\": {
        \"nodeSelector\": {\"${ML_NODE_SELECTOR_KEY}\": \"${ML_NODE_SELECTOR_VALUE}\"},
        \"tolerations\": [
          {\"key\": \"cni.istio.io/not-ready\", \"operator\": \"Exists\"},
          {\"key\": \"${ML_TOLERATION_KEY}\", \"operator\": \"Equal\", \"value\": \"${ML_TOLERATION_VALUE}\", \"effect\": \"${ML_TOLERATION_EFFECT}\"}
        ]
      }
    }
  }
}"

# Keep the intentionally CPU-heavy platform namespaces on the CPU pool.
# DataHub and recsys-dataflow are left untouched because their charts already target
# the CPU node pool in this environment. Observability is explicitly pinned here.
disable_sidecar_injection daemonset observability recsys-promtail
patch_deployment_cpu observability recsys-prometheus

kube_system_ml_deployments=(
  event-exporter-gke
  konnectivity-agent
  konnectivity-agent-autoscaler
  kube-dns-autoscaler
  metrics-server-v1.35.1
)
for deployment in "${kube_system_ml_deployments[@]}"; do
  patch_gke_managed_deployment_ml "${deployment}"
done

# kube-dns is GKE-managed and currently requests enough CPU that moving all replicas
# to the single ML node starves Ray/KFP scheduling. Keep it on the CPU pool until ML
# node quota is raised or a second ML node is available. kube-system DaemonSets are
# node agents and intentionally remain per-node.
patch_gke_managed_deployment_cpu kube-dns
# GKE reconciles custom tolerations off l7-default-backend. Pinning it to the
# tainted ML pool therefore creates a permanently Pending replacement pod.
patch_gke_managed_deployment_cpu l7-default-backend

for deployment in "${kubeflow_deployments[@]}"; do
  rollout_deployment kubeflow "${deployment}"
done
for deployment in "${kserve_deployments[@]}"; do
  rollout_deployment kserve "${deployment}"
done
for deployment in "${ci_cpu_deployments[@]}"; do
  rollout_deployment ci "${deployment}"
done
rollout_daemonset ci recsys-registry-node-proxy
for deployment in "${cert_manager_deployments[@]}"; do
  rollout_deployment cert-manager "${deployment}"
done
for deployment in "${external_secret_deployments[@]}"; do
  rollout_deployment external-secrets "${deployment}"
done
for deployment in "${experiment_tracking_deployments[@]}"; do
  rollout_deployment experiment-tracking "${deployment}"
done
for deployment in "${keda_deployments[@]}"; do
  rollout_deployment keda "${deployment}"
done
rollout_deployment ingress-nginx ingress-nginx-controller
rollout_deployment istio-system istiod
rollout_deployment observability recsys-prometheus
rollout_daemonset observability recsys-promtail
for deployment in "${kube_system_ml_deployments[@]}"; do
  rollout_deployment kube-system "${deployment}"
done
rollout_deployment kube-system kube-dns

echo "Rebalanced ML/control/tracking workloads and movable kube-system deployments onto ${ML_NODE_SELECTOR_KEY}=${ML_NODE_SELECTOR_VALUE}; kept datahub, observability, recsys-dataflow, kube-dns, and node-agent DaemonSets on ${CPU_NODE_SELECTOR_KEY}=${CPU_NODE_SELECTOR_VALUE} where required."
