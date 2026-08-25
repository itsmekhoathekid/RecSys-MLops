resource "kubernetes_namespace" "observability" {
  metadata {
    labels = {
      istio-injection = "enabled"
    }

    name = "observability"
  }

  depends_on = [google_container_node_pool.cpu]
}

resource "kubernetes_namespace" "experiment_tracking" {
  metadata {
    labels = {
      istio-injection = "enabled"
    }

    name = "experiment-tracking"
  }

  depends_on = [google_container_node_pool.cpu]
}

resource "kubernetes_namespace" "recsys_dataflow" {
  metadata {
    labels = {
      istio-injection = "enabled"
    }

    name = "recsys-dataflow"
  }

  depends_on = [google_container_node_pool.cpu]
}

resource "kubernetes_namespace" "datahub" {
  count = var.deploy_datahub ? 1 : 0

  metadata {
    labels = {
      istio-injection = "enabled"
    }

    name = "datahub"
  }

  depends_on = [google_container_node_pool.cpu]
}

resource "kubernetes_namespace" "kserve_triton_inference" {
  # Keep the namespace and its ExternalSecret available while model CD is
  # intentionally deferred; the online/inference APIs still depend on the
  # KServe control plane and Jenkins later installs the predictor here.
  count = var.deploy_serving || var.deploy_model_serving ? 1 : 0

  metadata {
    labels = {
      istio-injection = "enabled"
    }

    name = "kserve-triton-inference"
  }

  depends_on = [google_container_node_pool.ml_system]
}

resource "kubernetes_namespace" "api_serving" {
  count = var.deploy_serving || var.deploy_gateway ? 1 : 0

  metadata {
    labels = {
      istio-injection = "enabled"
    }

    name = "api-serving"
  }

  depends_on = [google_container_node_pool.ml_system]
}

resource "kubernetes_namespace" "llm_inference" {
  count = var.deploy_llm_inference ? 1 : 0

  metadata {
    labels = {
      istio-injection = "disabled"
    }

    name = "llm-inference"
  }

  depends_on = [
    google_container_node_pool.llm_cpu,
    google_container_node_pool.ml_system,
  ]
}

resource "kubernetes_labels" "ingress_nginx_mesh" {
  count = var.deploy_gateway && var.deploy_service_mesh ? 1 : 0

  api_version = "v1"
  kind        = "Namespace"

  metadata {
    name = "ingress-nginx"
  }

  labels = {
    istio-injection = "enabled"
  }

  force = true

  depends_on = [
    helm_release.ingress_nginx,
    helm_release.istiod,
  ]
}
