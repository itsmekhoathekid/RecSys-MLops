variable "kagent_version" {
  description = "Pinned kagent CRD and application chart version."
  type        = string
  default     = "0.9.9"
}

resource "kubernetes_namespace" "kagent" {
  count = var.deploy_llm_inference ? 1 : 0

  metadata {
    labels = {
      istio-injection = "disabled"
    }

    name = "kagent"
  }

  depends_on = [google_container_node_pool.ml_system]
}

# Development fallback only. With agentgateway authentication enabled, External
# Secrets creates this Secret from Vault and this Terraform resource has count 0.
resource "kubernetes_secret_v1" "kagent_agent_gateway" {
  count = var.deploy_llm_inference && !var.agent_gateway_auth_enabled ? 1 : 0

  metadata {
    name      = "kagent-agent-gateway"
    namespace = kubernetes_namespace.kagent[0].metadata[0].name
  }

  data = {
    AGENT_GATEWAY_API_KEY = "not-required-by-current-agentgateway"
  }

  type = "Opaque"
}

resource "helm_release" "kagent_crds" {
  count = var.deploy_llm_inference ? 1 : 0

  name       = "kagent-crds"
  repository = "oci://ghcr.io/kagent-dev/kagent/helm"
  chart      = "kagent-crds"
  version    = var.kagent_version
  namespace  = kubernetes_namespace.kagent[0].metadata[0].name
  atomic     = true
  wait       = true
  timeout    = 600

  set {
    name  = "kmcp.enabled"
    value = "false"
  }

  depends_on = [kubernetes_namespace.kagent]
}

resource "helm_release" "kagent" {
  count = var.deploy_llm_inference ? 1 : 0

  name       = "kagent"
  repository = "oci://ghcr.io/kagent-dev/kagent/helm"
  chart      = "kagent"
  version    = var.kagent_version
  namespace  = kubernetes_namespace.kagent[0].metadata[0].name
  atomic     = true
  wait       = true
  timeout    = 900
  values = [
    file("${path.module}/../../../configs/kagent/values.yaml"),
  ]

  depends_on = [
    helm_release.kagent_crds,
    helm_release.llm_d_router,
    helm_release.recsys_security,
    null_resource.recsys_external_secrets_ready,
    kubernetes_secret_v1.kagent_agent_gateway,
  ]
}

resource "helm_release" "recsys_kagent_agent" {
  count = var.deploy_llm_inference ? 1 : 0

  name      = "recsys-kagent-agent"
  chart     = "${local.helm_dir}/recsys-kagent-agent"
  namespace = kubernetes_namespace.kagent[0].metadata[0].name
  atomic    = true
  wait      = true
  timeout   = 600

  depends_on = [helm_release.kagent]
}
