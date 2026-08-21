variable "kagent_version" {
  description = "Pinned kagent CRD and application chart version."
  type        = string
  default     = "0.9.9"
}

variable "agent_substrate_version" {
  description = "Pinned Agent Substrate CRD and application chart version."
  type        = string
  default     = "0.0.6"
}

resource "kubernetes_namespace" "ate_system" {
  count = var.deploy_llm_inference ? 1 : 0

  metadata {
    name = "ate-system"
  }

  depends_on = [google_container_node_pool.ml_system]
}

resource "helm_release" "substrate_crds" {
  count = var.deploy_llm_inference ? 1 : 0

  name       = "substrate-crds"
  repository = "oci://ghcr.io/kagent-dev/substrate/helm"
  chart      = "substrate-crds"
  version    = var.agent_substrate_version
  namespace  = kubernetes_namespace.ate_system[0].metadata[0].name
  atomic     = true
  wait       = true
  timeout    = 600

  # Substrate 0.0.6 exposes replicas through /scale but omits the selector
  # required by HPA/KEDA. Keep the compatible runtime pin and backport the
  # additive scale-selector fields that landed upstream after this release.
  postrender {
    binary_path = "${path.module}/../../../ops/helm/substrate_crds_hpa_postrender.py"
    args        = ["workerpool-hpa-selector-v1"]
  }

  depends_on = [kubernetes_namespace.ate_system]
}

# Substrate 0.0.6 does not expose storageClassName values for its RustFS PVC or
# Valkey volumeClaimTemplates. Pre-creating the exact claims keeps the platform
# on quota-safe pd-standard disks instead of consuming the regional SSD quota.
resource "kubernetes_persistent_volume_claim_v1" "substrate_rustfs" {
  count = var.deploy_llm_inference ? 1 : 0

  metadata {
    name      = "rustfs-data"
    namespace = kubernetes_namespace.ate_system[0].metadata[0].name
    labels = {
      "app.kubernetes.io/managed-by" = "Helm"
    }
    annotations = {
      "meta.helm.sh/release-name"      = "substrate"
      "meta.helm.sh/release-namespace" = "ate-system"
    }
  }

  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = "standard"
    resources {
      requests = { storage = "1Gi" }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [kubernetes_namespace.ate_system]
}

resource "kubernetes_persistent_volume_claim_v1" "substrate_valkey" {
  for_each = var.deploy_llm_inference ? toset(["0", "1", "2", "3", "4", "5"]) : toset([])

  metadata {
    name      = "data-valkey-cluster-${each.key}"
    namespace = kubernetes_namespace.ate_system[0].metadata[0].name
    labels = {
      app = "valkey-cluster"
    }
  }

  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = "standard"
    resources {
      requests = { storage = "1Gi" }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [kubernetes_namespace.ate_system]
}

resource "helm_release" "substrate" {
  count = var.deploy_llm_inference ? 1 : 0

  name       = "substrate"
  repository = "oci://ghcr.io/kagent-dev/substrate/helm"
  chart      = "substrate"
  version    = var.agent_substrate_version
  namespace  = kubernetes_namespace.ate_system[0].metadata[0].name
  atomic     = true
  wait       = true
  timeout    = 900

  # GKE projected service-account tokens use the cluster OIDC issuer rather
  # than Kubernetes' generic in-cluster URL. Substrate must validate the same
  # issuer used by kagent and the ActorTemplate controller tokens.
  set {
    name = "auth.jwt.issuer"
    value = format(
      "https://container.googleapis.com/v1/projects/%s/locations/%s/clusters/%s",
      var.project_id,
      var.zone,
      google_container_cluster.recsys.name,
    )
  }

  postrender {
    binary_path = "${path.module}/../../../ops/helm/substrate_gke_postrender.py"
    args        = ["gke-public-oidc-v1"]
  }

  depends_on = [
    helm_release.substrate_crds,
    kubernetes_persistent_volume_claim_v1.substrate_rustfs,
    kubernetes_persistent_volume_claim_v1.substrate_valkey,
  ]
}

resource "kubernetes_namespace" "kagent" {
  count = var.deploy_llm_inference ? 1 : 0

  metadata {
    name = "kagent"
    labels = {
      # Keep sidecar injection opt-in per workload; the MCP chart owns its pod
      # annotation and the Substrate worker pool must not be mutated by Istio.
      "istio-injection" = "disabled"
    }
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

  postrender {
    binary_path = "${path.module}/../../../ops/helm/kagent_workerpool_hpa_postrender.py"
    args        = ["workerpool-hpa-selector-v1"]
  }

  depends_on = [
    helm_release.kagent_crds,
    helm_release.substrate,
    helm_release.llm_d_router,
    helm_release.recsys_security,
    null_resource.recsys_external_secrets_ready,
    kubernetes_secret_v1.kagent_agent_gateway,
  ]
}
