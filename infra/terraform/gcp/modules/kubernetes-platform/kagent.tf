locals {
  # Use the official, mutually tested kagent/Substrate release pair. No local
  # source patch, private runtime image or post-renderer participates here.
  kagent_chart_repository = "oci://ghcr.io/kagent-dev/kagent/helm"
}

resource "kubernetes_namespace" "ate_system" {
  count = var.config.deploy_llm_inference ? 1 : 0

  metadata {
    name = "ate-system"
  }

}

resource "helm_release" "substrate_crds" {
  count = var.config.deploy_llm_inference ? 1 : 0

  name       = "substrate-crds"
  repository = "oci://ghcr.io/kagent-dev/substrate/helm"
  chart      = "substrate-crds"
  version    = var.config.agent_substrate_version
  namespace  = kubernetes_namespace.ate_system[0].metadata[0].name
  atomic     = true
  wait       = true
  timeout    = 600

  depends_on = [kubernetes_namespace.ate_system]
}

# Pre-create the exact RustFS and Valkey claims on quota-safe pd-standard
# storage. Official Substrate 0.0.9 owns the workloads and reuses these claims.
resource "kubernetes_persistent_volume_claim_v1" "substrate_rustfs" {
  count = var.config.deploy_llm_inference ? 1 : 0

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
      requests = { storage = "10Gi" }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [kubernetes_namespace.ate_system]
}

resource "kubernetes_persistent_volume_claim_v1" "substrate_valkey" {
  for_each = var.config.deploy_llm_inference ? toset(["0", "1", "2", "3", "4", "5"]) : toset([])

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
  count = var.config.deploy_llm_inference ? 1 : 0

  name       = "substrate"
  repository = "oci://ghcr.io/kagent-dev/substrate/helm"
  chart      = "substrate"
  version    = var.config.agent_substrate_version
  namespace  = kubernetes_namespace.ate_system[0].metadata[0].name
  atomic     = true
  wait       = true
  timeout    = 900

  # Sandbox images live in the private regional Artifact Registry. atelet
  # performs the pull itself (outside kubelet), so it must use GCP ADC from
  # the node/workload identity instead of making an anonymous registry call.
  set {
    name  = "atelet.gcpAuthForImagePulls"
    value = "true"
  }

  set {
    name  = "auth.mode"
    value = "jwt"
  }

  set {
    name  = "auth.jwt.issuer"
    value = "https://container.googleapis.com/v1/projects/${var.config.project_id}/locations/${var.config.region}-b/clusters/${var.config.cluster_name}"
  }

  # Reuse the existing six Valkey PVCs without downgrading their on-disk AOF
  # format. This is a supported upstream chart value, not a runtime patch.
  set {
    name  = "images.valkey"
    value = "valkey/valkey:9.1@sha256:4963247afc4cd33c7d3b2d2816b9f7f8eeebab148d29056c2ca4d7cbc966f2d9"
  }

  # The production RustFS claim has already been expanded to 10Gi. Pin the
  # upstream chart to the same size so Helm never attempts an illegal shrink.
  set {
    name  = "rustfs.storageSize"
    value = "10Gi"
  }

  depends_on = [
    helm_release.substrate_crds,
    kubernetes_persistent_volume_claim_v1.substrate_rustfs,
  ]
}

resource "kubernetes_namespace" "kagent" {
  # Deliberately keep the namespace outside the generic feature-disable
  # transaction. MCP workloads are Jenkins-owned and the live Terraform guard
  # must inspect them before any teardown can remove their namespace. A full
  # namespace deletion therefore belongs to the separately reviewed teardown.
  count = 1

  metadata {
    name = "kagent"
    # Leave the namespace unlabeled so Istio's pod annotation can opt the MCP
    # workload in. Sandbox/WorkerPool pods have no injection annotation and
    # therefore remain outside the mesh.
  }

}

# Development fallback only. With agentgateway authentication enabled, External
# Secrets creates this Secret from Vault and this Terraform resource has count 0.
resource "kubernetes_secret_v1" "kagent_agent_gateway" {
  count = var.config.deploy_llm_inference && !var.config.agent_gateway_auth_enabled ? 1 : 0

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
  count = var.config.deploy_llm_inference ? 1 : 0

  name       = "kagent-crds"
  repository = local.kagent_chart_repository
  chart      = "kagent-crds"
  version    = var.config.kagent_version
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
  count = var.config.deploy_llm_inference ? 1 : 0

  name       = "kagent"
  repository = local.kagent_chart_repository
  chart      = "kagent"
  version    = var.config.kagent_version
  namespace  = kubernetes_namespace.kagent[0].metadata[0].name
  atomic     = true
  wait       = true
  timeout    = 900
  values = [
    file("${var.repo_root}/configs/kagent/values.yaml"),
  ]

  depends_on = [
    helm_release.kagent_crds,
    helm_release.substrate,
    helm_release.llm_d_router,
    helm_release.recsys_security,
    null_resource.recsys_external_secrets_ready,
    kubernetes_secret_v1.kagent_agent_gateway,
  ]
}

# A dedicated pool keeps recommendation traffic and its KEDA lifecycle isolated
# from the context/RAG sandbox. KEDA owns the /scale subresource; Terraform owns
# immutable runtime/security fields and deliberately ignores live replica drift.
resource "kubernetes_manifest" "recsys_recommendation_sandbox_pool" {
  count = var.config.deploy_llm_inference ? 1 : 0

  manifest = {
    apiVersion = "ate.dev/v1alpha1"
    kind       = "WorkerPool"
    metadata = {
      name      = "recsys-recommendation-sandbox-pool"
      namespace = kubernetes_namespace.kagent[0].metadata[0].name
      labels = {
        "app.kubernetes.io/part-of" = "recsys-agentic"
        "ate.dev/worker-pool"       = "recsys-recommendation-sandbox-pool"
        "kagent.dev/worker-pool"    = "recsys-recommendation-sandbox-pool"
      }
    }
    spec = {
      replicas     = 1
      ateomImage   = "ghcr.io/kagent-dev/substrate/ateom-gvisor:v${var.config.agent_substrate_version}"
      sandboxClass = "gvisor"
      template = {
        nodeSelector = {
          "cloud.google.com/gke-nodepool" = var.cluster.cpu_node_pool_name
        }
        resources = {
          requests = {
            cpu    = "250m"
            memory = "1Gi"
          }
          limits = {
            memory = "2Gi"
          }
        }
      }
    }
  }

  computed_fields = ["spec.replicas"]

  # Terraform owns the immutable runtime image. Operational replica changes
  # are ignored above, while stale kubectl field ownership must not block a
  # pinned Substrate upgrade.
  field_manager {
    force_conflicts = true
  }

  depends_on = [helm_release.kagent]
}

# The coordinator has a dedicated pool so orchestration traffic cannot consume
# the two specialist pools. KEDA owns replicas through WorkerPool /scale while
# Terraform owns the immutable Substrate runtime and scheduler labels.
resource "kubernetes_manifest" "recsys_coordinator_sandbox_pool" {
  count = var.config.deploy_llm_inference ? 1 : 0

  manifest = {
    apiVersion = "ate.dev/v1alpha1"
    kind       = "WorkerPool"
    metadata = {
      name      = "recsys-coordinator-sandbox-pool"
      namespace = kubernetes_namespace.kagent[0].metadata[0].name
      labels = {
        "app.kubernetes.io/part-of" = "recsys-agentic"
        "ate.dev/worker-pool"       = "recsys-coordinator-sandbox-pool"
        "kagent.dev/worker-pool"    = "recsys-coordinator-sandbox-pool"
      }
    }
    spec = {
      replicas     = 1
      ateomImage   = "ghcr.io/kagent-dev/substrate/ateom-gvisor:v${var.config.agent_substrate_version}"
      sandboxClass = "gvisor"
      template = {
        nodeSelector = {
          "cloud.google.com/gke-nodepool" = var.cluster.cpu_node_pool_name
        }
        resources = {
          requests = {
            cpu    = "250m"
            memory = "1Gi"
          }
          limits = {
            memory = "2Gi"
          }
        }
      }
    }
  }

  computed_fields = ["spec.replicas"]

  field_manager {
    force_conflicts = true
  }

  depends_on = [helm_release.kagent]
}

resource "kubernetes_cluster_role_v1" "keda_workerpool_scaler" {
  count = var.config.deploy_llm_inference ? 1 : 0

  metadata {
    name = "keda-ate-workerpool-scaler"
  }

  rule {
    api_groups = ["ate.dev"]
    resources  = ["workerpools"]
    verbs      = ["get", "list", "watch"]
  }

  rule {
    api_groups = ["ate.dev"]
    resources  = ["workerpools/scale"]
    verbs      = ["get", "patch", "update"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "keda_workerpool_scaler" {
  count = var.config.deploy_llm_inference ? 1 : 0

  metadata {
    name = "keda-ate-workerpool-scaler"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.keda_workerpool_scaler[0].metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "keda-operator"
    namespace = "keda"
  }

  depends_on = [helm_release.keda]
}
