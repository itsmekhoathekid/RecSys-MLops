resource "kubernetes_namespace" "agentregistry" {
  count = var.config.deploy_agent_registry ? 1 : 0

  metadata {
    name = "agentregistry"
    labels = {
      istio-injection = "disabled"
    }
  }

  lifecycle {
    precondition {
      condition     = var.config.deploy_vault && var.config.deploy_llm_inference
      error_message = "deploy_agent_registry requires deploy_vault=true and deploy_llm_inference=true so Vault/External Secrets and kagent are available."
    }
  }

}

resource "helm_release" "agentregistry_postgres" {
  count = var.config.deploy_agent_registry ? 1 : 0

  name      = "agentregistry-postgres"
  chart     = "${local.helm_dir}/recsys-agent-registry-postgres"
  namespace = kubernetes_namespace.agentregistry[0].metadata[0].name
  atomic    = true
  wait      = true
  timeout   = 600

  depends_on = [
    helm_release.recsys_security,
    null_resource.recsys_external_secrets_ready,
  ]
}

resource "helm_release" "agentregistry" {
  count = var.config.deploy_agent_registry ? 1 : 0

  name       = "agentregistry"
  repository = "oci://ghcr.io/agentregistry-dev/agentregistry/charts"
  chart      = "agentregistry"
  version    = var.config.agentregistry_version
  namespace  = kubernetes_namespace.agentregistry[0].metadata[0].name
  atomic     = true
  wait       = true
  timeout    = 900

  values = [
    file("${var.repo_root}/configs/agentregistry/values.yaml"),
  ]

  depends_on = [
    helm_release.agentregistry_postgres,
    helm_release.kagent,
  ]
}
