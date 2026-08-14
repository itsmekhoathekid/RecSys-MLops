variable "deploy_agent_registry" {
  description = "Deploy the Agent Registry catalog with a persistent pgvector database."
  type        = bool
  default     = false
}

variable "agentregistry_version" {
  description = "Pinned official Agent Registry OCI Helm chart version."
  type        = string
  default     = "0.4.0"
}

resource "kubernetes_namespace" "agentregistry" {
  count = var.deploy_agent_registry ? 1 : 0

  metadata {
    name = "agentregistry"
    labels = {
      istio-injection = "disabled"
    }
  }

  lifecycle {
    precondition {
      condition     = var.deploy_vault && var.deploy_llm_inference
      error_message = "deploy_agent_registry requires deploy_vault=true and deploy_llm_inference=true so Vault/External Secrets and kagent are available."
    }
  }

  depends_on = [google_container_node_pool.cpu]
}

resource "helm_release" "agentregistry_postgres" {
  count = var.deploy_agent_registry ? 1 : 0

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
  count = var.deploy_agent_registry ? 1 : 0

  name       = "agentregistry"
  repository = "oci://ghcr.io/agentregistry-dev/agentregistry/charts"
  chart      = "agentregistry"
  version    = var.agentregistry_version
  namespace  = kubernetes_namespace.agentregistry[0].metadata[0].name
  atomic     = true
  wait       = true
  timeout    = 900

  values = [
    file("${path.module}/../../../configs/agentregistry/values.yaml"),
  ]

  depends_on = [
    helm_release.agentregistry_postgres,
    helm_release.kagent,
  ]
}

