resource "kubernetes_namespace" "observability" {
  metadata {
    labels = {
      istio-injection = "enabled"
    }

    name = "observability"
  }

}

resource "kubernetes_namespace" "langfuse" {
  count = var.config.deploy_langfuse ? 1 : 0

  metadata {
    labels = {
      # Langfuse uses explicit NetworkPolicy boundaries and must not inherit
      # the production Istio request path or sidecar resource overhead.
      istio-injection = "disabled"
    }

    name = "langfuse"
  }
}

resource "kubernetes_namespace" "experiment_tracking" {
  metadata {
    labels = {
      istio-injection = "enabled"
    }

    name = "experiment-tracking"
  }

}

resource "kubernetes_namespace" "recsys_dataflow" {
  metadata {
    labels = {
      istio-injection = "enabled"
    }

    name = "recsys-dataflow"
  }

}

resource "kubernetes_namespace" "datahub" {
  count = var.config.deploy_datahub ? 1 : 0

  metadata {
    labels = {
      istio-injection = "enabled"
    }

    name = "datahub"
  }

}

resource "kubernetes_namespace" "kserve_triton_inference" {
  # Keep the namespace and its ExternalSecret available while model CD is
  # intentionally deferred; the online/inference APIs still depend on the
  # KServe control plane and Jenkins later installs the predictor here.
  count = var.config.deploy_serving || var.config.deploy_model_serving ? 1 : 0

  metadata {
    labels = {
      istio-injection = "enabled"
    }

    name = "kserve-triton-inference"
  }

}

resource "kubernetes_namespace" "api_serving" {
  count = var.config.deploy_serving || var.config.deploy_gateway ? 1 : 0

  metadata {
    labels = {
      istio-injection = "enabled"
    }

    name = "api-serving"
  }

}

resource "kubernetes_namespace" "llm_inference" {
  count = var.config.deploy_llm_inference ? 1 : 0

  metadata {
    labels = {
      istio-injection = "disabled"
    }

    name = "llm-inference"
  }

}

resource "kubernetes_labels" "ingress_nginx_mesh" {
  count = var.config.deploy_gateway && var.config.deploy_service_mesh ? 1 : 0

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
