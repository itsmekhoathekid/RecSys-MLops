resource "null_resource" "cluster_credentials" {
  triggers = {
    cluster_id = google_container_cluster.recsys.id
    endpoint   = google_container_cluster.recsys.endpoint
  }

  provisioner "local-exec" {
    command = "gcloud container clusters get-credentials ${google_container_cluster.recsys.name} --zone ${var.zone} --project ${var.project_id}"
  }

  depends_on = [
    google_container_node_pool.cpu,
    google_container_node_pool.ml_system,
    google_container_node_pool.gpu,
  ]
}

resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  namespace        = "cert-manager"
  create_namespace = true
  wait             = true
  timeout          = 600

  set {
    name  = "installCRDs"
    value = "true"
  }

  depends_on = [google_container_node_pool.cpu]
}

resource "helm_release" "keda" {
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  namespace        = "keda"
  create_namespace = true
  wait             = true
  timeout          = 600

  depends_on = [google_container_node_pool.cpu]
}

resource "helm_release" "keda_http" {
  name       = "keda-add-ons-http"
  repository = "https://kedacore.github.io/charts"
  chart      = "keda-add-ons-http"
  namespace  = "keda"
  wait       = true
  timeout    = 600

  depends_on = [helm_release.keda]
}

resource "helm_release" "external_secrets" {
  count = var.deploy_service_mesh ? 1 : 0

  name             = "external-secrets"
  repository       = "https://charts.external-secrets.io"
  chart            = "external-secrets"
  namespace        = "external-secrets"
  create_namespace = true
  wait             = true
  timeout          = 600

  set {
    name  = "installCRDs"
    value = "true"
  }

  depends_on = [google_container_node_pool.cpu]
}

resource "helm_release" "kuberay_operator" {
  name             = "kuberay-operator"
  repository       = "https://ray-project.github.io/kuberay-helm/"
  chart            = "kuberay-operator"
  namespace        = "kubeflow"
  create_namespace = true
  wait             = true
  timeout          = 600

  depends_on = [google_container_node_pool.cpu]
}

resource "helm_release" "istio_base" {
  count = var.deploy_service_mesh ? 1 : 0

  name             = "istio-base"
  repository       = "https://istio-release.storage.googleapis.com/charts"
  chart            = "base"
  namespace        = "istio-system"
  create_namespace = true
  wait             = true
  timeout          = 600

  depends_on = [
    google_container_node_pool.cpu,
    null_resource.cluster_credentials,
  ]
}

resource "helm_release" "istiod" {
  count = var.deploy_service_mesh ? 1 : 0

  name       = "istiod"
  repository = "https://istio-release.storage.googleapis.com/charts"
  chart      = "istiod"
  namespace  = "istio-system"
  wait       = true
  timeout    = 600

  set {
    name  = "global.configValidation"
    value = "false"
  }

  depends_on = [
    helm_release.istio_base,
  ]
}

resource "helm_release" "ingress_nginx" {
  count = var.deploy_gateway ? 1 : 0

  name             = "ingress-nginx"
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  namespace        = "ingress-nginx"
  create_namespace = true
  wait             = true
  timeout          = 600

  set {
    name  = "controller.service.type"
    value = "LoadBalancer"
  }

  set {
    name  = "controller.config.limit-req-status-code"
    value = "429"
  }

  set {
    name  = "controller.config.limit-conn-status-code"
    value = "429"
  }

  depends_on = [google_container_node_pool.cpu]
}

resource "null_resource" "kubeflow_pipelines" {
  count = var.install_kubeflow_pipelines ? 1 : 0

  triggers = {
    cluster_id = google_container_cluster.recsys.id
    version    = var.kubeflow_pipelines_version
  }

  provisioner "local-exec" {
    command     = <<-EOT
      set -euo pipefail
      kubectl apply -k "github.com/kubeflow/pipelines/manifests/kustomize/cluster-scoped-resources?ref=${var.kubeflow_pipelines_version}"
      kubectl wait --for condition=established --timeout=120s crd/applications.app.k8s.io
      kubectl apply -k "github.com/kubeflow/pipelines/manifests/kustomize/env/dev?ref=${var.kubeflow_pipelines_version}"
      kubectl rollout status deploy/ml-pipeline -n kubeflow --timeout=600s
      kubectl rollout status deploy/ml-pipeline-ui -n kubeflow --timeout=600s
      kubectl rollout status deploy/workflow-controller -n kubeflow --timeout=600s
      if [ "${var.scale_optional_kfp_components}" = "true" ]; then
        kubectl scale deploy/metadata-writer -n kubeflow --replicas=0 >/dev/null 2>&1 || true
        kubectl scale deploy/proxy-agent -n kubeflow --replicas=0 >/dev/null 2>&1 || true
      fi
    EOT
    interpreter = ["/bin/bash", "-c"]
  }

  depends_on = [
    null_resource.cluster_credentials,
    helm_release.kuberay_operator,
  ]
}

resource "null_resource" "kserve" {
  count = var.install_kserve ? 1 : 0

  triggers = {
    cluster_id = google_container_cluster.recsys.id
    version    = var.kserve_version
  }

  provisioner "local-exec" {
    command     = <<-EOT
      set -euo pipefail
      kubectl apply --server-side --force-conflicts -f "https://github.com/kserve/kserve/releases/download/${var.kserve_version}/kserve.yaml"
      kubectl rollout status deploy/kserve-controller-manager -n kserve --timeout=600s
      for _ in $(seq 1 60); do
        if kubectl get endpoints kserve-webhook-server-service -n kserve -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null | grep -q .; then
          break
        fi
        sleep 2
      done
      kubectl apply --server-side --force-conflicts -f "https://github.com/kserve/kserve/releases/download/${var.kserve_version}/kserve-cluster-resources.yaml"
      kubectl get clusterservingruntime kserve-tritonserver
    EOT
    interpreter = ["/bin/bash", "-c"]
  }

  depends_on = [
    null_resource.cluster_credentials,
    helm_release.cert_manager,
  ]
}
