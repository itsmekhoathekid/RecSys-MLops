check "agent_gateway_auth_dependencies" {
  assert {
    condition = (
      !var.config.deploy_llm_inference ||
      !var.config.agent_gateway_auth_enabled ||
      (var.config.deploy_vault && var.config.deploy_service_mesh)
    )
    error_message = "agent_gateway_auth_enabled requires deploy_vault=true and deploy_service_mesh=true so Vault and External Secrets Operator can supply the API key."
  }
}

resource "null_resource" "llm_gateway_api_crds" {
  count = var.config.deploy_llm_inference ? 1 : 0

  triggers = {
    cluster_id          = var.cluster.id
    gateway_api_version = var.config.gateway_api_version
    gaie_version        = var.config.gateway_api_inference_extension_version
  }

  provisioner "local-exec" {
    command = "bash ${var.repo_root}/ops/gcp/install_llm_gateway_crds.sh"
    environment = {
      GATEWAY_API_VERSION = var.config.gateway_api_version
      GAIE_VERSION        = var.config.gateway_api_inference_extension_version
    }
  }

}

resource "helm_release" "agentgateway_crds" {
  count = var.config.deploy_llm_inference ? 1 : 0

  name             = "agentgateway-crds"
  repository       = "oci://cr.agentgateway.dev/charts"
  chart            = "agentgateway-crds"
  version          = var.config.agentgateway_version
  namespace        = "agentgateway-system"
  create_namespace = true
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 600

  depends_on = [
    null_resource.llm_gateway_api_crds,
  ]
}

resource "helm_release" "agentgateway" {
  count = var.config.deploy_llm_inference ? 1 : 0

  name             = "agentgateway"
  repository       = "oci://cr.agentgateway.dev/charts"
  chart            = "agentgateway"
  version          = var.config.agentgateway_version
  namespace        = "agentgateway-system"
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 600
  values = [
    file("${var.repo_root}/configs/llm-d/agentgateway-values.yaml"),
  ]

  depends_on = [helm_release.agentgateway_crds]
}

resource "helm_release" "recsys_llm_serving" {
  count = var.config.deploy_llm_inference ? 1 : 0

  name             = "recsys-llm-serving"
  chart            = "${local.helm_dir}/recsys-llm-serving"
  namespace        = kubernetes_namespace.llm_inference[0].metadata[0].name
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 1200
  values = concat([
    file(
      var.config.llm_node_pool_mode == "cpu-services-shared"
      ? "${local.helm_dir}/recsys-llm-serving/values-cpu-shared.yaml"
      : "${local.helm_dir}/recsys-llm-serving/values-gcp.yaml"
    ),
    file(
      var.config.llm_optimization_profile == "optimized"
      ? "${local.helm_dir}/recsys-llm-serving/values-optimized.yaml"
      : "${local.helm_dir}/recsys-llm-serving/values-baseline.yaml"
    ),
    ], var.config.capacity_profile == "compact-12vcpu" ? [
    file("${local.helm_dir}/recsys-llm-serving/values-compact-12vcpu.yaml"),
  ] : [])

  set {
    name  = "gateway.auth.enabled"
    value = tostring(var.config.agent_gateway_auth_enabled)
  }

  depends_on = [
    helm_release.agentgateway,
    helm_release.recsys_security,
    null_resource.recsys_external_secrets_ready,
    kubernetes_namespace.llm_inference,
  ]
}

resource "helm_release" "llm_d_router" {
  count = var.config.deploy_llm_inference ? 1 : 0

  name             = "llm-d-optimized-baseline"
  repository       = "oci://ghcr.io/llm-d/charts"
  chart            = "llm-d-router-gateway"
  version          = var.config.llm_d_router_chart_version
  namespace        = kubernetes_namespace.llm_inference[0].metadata[0].name
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900
  values = [
    file(
      var.config.llm_optimization_profile == "optimized"
      ? "${var.repo_root}/configs/llm-d/router-llama-cpp-cpu-optimized-values.yaml"
      : "${var.repo_root}/configs/llm-d/router-llama-cpp-cpu-baseline-values.yaml"
    ),
  ]

  depends_on = [
    helm_release.agentgateway,
    helm_release.recsys_llm_serving,
  ]
}
