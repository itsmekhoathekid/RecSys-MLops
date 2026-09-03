locals {
  kagent_source_commit    = "e6df917e9fa8"
  kagent_artifact_prefix  = "kagent-e6df917"
  kagent_image_version    = "0.10.0-e6df917-substrate0011-v8"
  kagent_registry         = "${var.config.region}-docker.pkg.dev"
  kagent_image_repository = "${var.config.project_id}/${var.config.artifact_registry_repository}/${local.kagent_artifact_prefix}"
  kagent_chart_repository = "oci://${local.image_repo}/${local.kagent_artifact_prefix}/helm"
}

resource "kubernetes_namespace" "ate_system" {
  count = var.config.deploy_llm_inference ? 1 : 0

  metadata {
    name = "ate-system"
  }

}

resource "kubernetes_namespace" "podcertificate_controller_system" {
  count = var.config.deploy_llm_inference ? 1 : 0

  metadata {
    name = "podcertificate-controller-system"
    labels = {
      "app.kubernetes.io/managed-by" = "Helm"
    }
    annotations = {
      "meta.helm.sh/release-name"      = "substrate"
      "meta.helm.sh/release-namespace" = "ate-system"
    }
  }

}

resource "helm_release" "substrate_mtls_bootstrap" {
  count = var.config.deploy_llm_inference ? 1 : 0

  name      = "substrate-mtls-bootstrap"
  chart     = "${var.helm_dir}/substrate-mtls-bootstrap"
  namespace = kubernetes_namespace.ate_system[0].metadata[0].name
  atomic    = true
  wait      = true
  timeout   = 300

  depends_on = [
    kubernetes_namespace.ate_system,
    kubernetes_namespace.podcertificate_controller_system,
  ]
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

# Substrate does not expose storageClassName values for its RustFS PVC or Valkey
# volumeClaimTemplates. Pre-creating the exact claims keeps the platform on
# quota-safe pd-standard disks instead of consuming the regional SSD quota.
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
      requests = { storage = "1Gi" }
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

  set {
    name  = "auth.mode"
    value = "mtls"
  }

  # GKE projected service-account tokens use the cluster OIDC issuer rather
  # than Kubernetes' generic in-cluster URL. Substrate must validate the same
  # issuer used by kagent and the ActorTemplate controller tokens.
  set {
    name = "auth.jwt.issuer"
    value = format(
      "https://container.googleapis.com/v1/projects/%s/locations/%s/clusters/%s",
      var.config.project_id,
      var.config.zone,
      var.cluster.name,
    )
  }

  # Valkey is served with the service-DNS certificate in mTLS mode. ateapi
  # authenticates with its pod-identity bundle while validating that stable
  # service name instead of a StatefulSet pod address.
  set {
    name  = "redis.clientCert"
    value = "/run/podidentity.podcert.ate.dev/credential-bundle.pem"
  }

  set {
    name  = "redis.tlsServerName"
    value = "valkey-cluster-service.ate-system.svc"
  }

  # Substrate 0.0.11 upgraded the persistent AOF format. Keep the 9.1 storage
  # engine after rolling the control plane back to 0.0.6; Valkey 8.0 cannot
  # read the resulting appendonly.aof.11.base.rdb files. This avoids restoring
  # the six PD snapshots solely to downgrade the storage binary.
  set {
    name  = "images.valkey"
    value = "valkey/valkey:9.1@sha256:4963247afc4cd33c7d3b2d2816b9f7f8eeebab148d29056c2ca4d7cbc966f2d9"
  }

  # The chart's gateways/routes schema is normalized by the GKE post-renderer;
  # retain the 0.0.11-bundled image for PodCertificate ECDSA support.
  set {
    name  = "images.agentgateway"
    value = "cr.agentgateway.dev/agentgateway:v1.4.1"
  }

  # Sandbox images live in the private regional Artifact Registry. atelet
  # performs the pull itself (outside kubelet), so it must use GCP ADC from
  # the node/workload identity instead of making an anonymous registry call.
  set {
    name  = "atelet.gcpAuthForImagePulls"
    value = "true"
  }

  postrender {
    binary_path = "${var.repo_root}/ops/helm/substrate_gke_postrender.py"
    args        = ["gke-mtls-0011-v1"]
  }

  depends_on = [
    helm_release.substrate_crds,
    helm_release.substrate_mtls_bootstrap,
    kubernetes_persistent_volume_claim_v1.substrate_rustfs,
    kubernetes_persistent_volume_claim_v1.substrate_valkey,
  ]
}

resource "kubernetes_namespace" "kagent" {
  count = var.config.deploy_llm_inference ? 1 : 0

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
  # The private OCI chart requires registry credentials at plan/apply time.
  # Compact recovery changes only replica count, so the orchestration script
  # scales the existing controller after Terraform rather than pulling the
  # chart again.
  values = [file("${var.repo_root}/configs/kagent/values.yaml")]

  set {
    name  = "registry"
    value = local.kagent_registry
  }

  set {
    name  = "tag"
    value = local.kagent_image_version
  }

  # Also records the compatibility build on the pod template. Besides making
  # provenance visible, this forces Helm to reconcile if an interrupted OCI
  # pull ever advances Terraform state without updating the live release.
  set {
    name  = "controller.podAnnotations.recsys\\.ai/compatibility-build"
    value = local.kagent_image_version
  }

  set {
    name  = "controller.image.repository"
    value = "${local.kagent_image_repository}/controller"
  }

  set {
    name  = "controller.goAgentImage.repository"
    value = "${local.kagent_image_repository}/golang-adk"
  }

  set {
    name  = "controller.skillsInitImage.repository"
    value = "${local.kagent_image_repository}/skills-init"
  }

  set {
    name  = "ui.image.repository"
    value = "${local.kagent_image_repository}/ui"
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
            cpu    = "1"
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
            cpu    = "1"
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
