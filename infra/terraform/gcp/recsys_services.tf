resource "helm_release" "recsys_observability" {
  name             = "recsys-observability"
  chart            = "${local.helm_dir}/recsys-observability"
  namespace        = "observability"
  create_namespace = true
  wait             = true
  timeout          = 900

  values = [
    file("${local.helm_dir}/recsys-observability/values-gcp.yaml"),
  ]

  depends_on = [google_container_node_pool.cpu]
}

resource "helm_release" "recsys_mlflow" {
  name             = "recsys-mlflow"
  chart            = "${local.helm_dir}/mlflow-stack"
  namespace        = "experiment-tracking"
  create_namespace = true
  wait             = true
  timeout          = 900

  values = [
    file("${local.helm_dir}/mlflow-stack/values-gcp.yaml"),
  ]

  dynamic "set" {
    for_each = local.mlflow_sets
    content {
      name  = set.key
      value = set.value
    }
  }

  set_sensitive {
    name  = "minio.rootPassword"
    value = random_password.minio_root.result
  }

  set_sensitive {
    name  = "postgres.password"
    value = random_password.mlflow_postgres.result
  }

  depends_on = [google_container_node_pool.cpu]
}

resource "helm_release" "recsys_runtime" {
  name             = "recsys-runtime"
  chart            = "${local.helm_dir}/recsys-runtime"
  namespace        = "kubeflow"
  create_namespace = true
  wait             = true
  timeout          = 600

  values = [
    file("${local.helm_dir}/recsys-runtime/values-gcp.yaml"),
  ]

  dynamic "set" {
    for_each = local.runtime_sets
    content {
      name  = set.key
      value = set.value
    }
  }

  set_sensitive {
    name  = "secret.minioRootPassword"
    value = random_password.minio_root.result
  }

  set_sensitive {
    name  = "secret.modelRegistryPostgresUri"
    value = "postgresql://mlflow:${random_password.mlflow_postgres.result}@postgres.experiment-tracking.svc.cluster.local:5432/mlflow"
  }

  depends_on = [
    helm_release.recsys_mlflow,
    null_resource.kubeflow_pipelines,
  ]
}

resource "helm_release" "recsys_data_platform" {
  name             = "recsys-data-platform"
  chart            = "${local.helm_dir}/recsys-data-platform"
  namespace        = "recsys-dataflow"
  create_namespace = true
  wait             = true
  timeout          = 1200

  values = [
    file("${local.helm_dir}/recsys-data-platform/values-gcp.yaml"),
  ]

  dynamic "set" {
    for_each = local.data_platform_sets
    content {
      name  = set.key
      value = set.value
    }
  }

  set_sensitive {
    name  = "minio.rootPassword"
    value = random_password.minio_root.result
  }

  set_sensitive {
    name  = "sourcePostgres.password"
    value = random_password.source_postgres.result
  }

  set_sensitive {
    name  = "airflowPostgres.password"
    value = random_password.airflow_postgres.result
  }

  depends_on = [
    helm_release.recsys_observability,
    google_container_node_pool.cpu,
  ]
}

resource "helm_release" "recsys_serving" {
  name             = "recsys-serving"
  chart            = "${local.helm_dir}/recsys-serving"
  namespace        = "kserve-triton-inference"
  create_namespace = true
  wait             = true
  timeout          = 1200

  values = [
    file("${local.helm_dir}/recsys-serving/values-gcp-gpu.yaml"),
  ]

  dynamic "set" {
    for_each = local.serving_sets
    content {
      name  = set.key
      value = set.value
    }
  }

  set_sensitive {
    name  = "kserve.secret.secretAccessKey"
    value = random_password.minio_root.result
  }

  depends_on = [
    helm_release.keda_http,
    helm_release.recsys_mlflow,
    helm_release.recsys_data_platform,
    null_resource.kserve,
    google_container_node_pool.gpu,
  ]
}

resource "helm_release" "recsys_ray_gpu" {
  count = var.deploy_ray_job ? 1 : 0

  name             = "recsys-ray-gpu"
  chart            = "${local.helm_dir}/ray-cluster"
  namespace        = "kubeflow"
  create_namespace = true
  wait             = false
  timeout          = 600

  values = [
    file("${local.helm_dir}/ray-cluster/values-gcp-gpu.yaml"),
  ]

  dynamic "set" {
    for_each = local.ray_sets
    content {
      name  = set.key
      value = set.value
    }
  }

  depends_on = [
    helm_release.kuberay_operator,
    helm_release.recsys_runtime,
    helm_release.recsys_data_platform,
    google_container_node_pool.gpu,
  ]
}

resource "helm_release" "recsys_gateway" {
  count = var.deploy_gateway ? 1 : 0

  name             = "recsys-gateway"
  chart            = "${local.helm_dir}/recsys-gateway"
  namespace        = "api-serving"
  create_namespace = true
  wait             = true
  timeout          = 600

  set {
    name  = "gateway.domain"
    value = var.gateway_domain
  }

  set {
    name  = "api.host"
    value = "api.${var.gateway_domain}"
  }

  set {
    name  = "grafana.host"
    value = "grafana.${var.gateway_domain}"
  }

  set {
    name  = "logs.host"
    value = "logs.${var.gateway_domain}"
  }

  set {
    name  = "traces.host"
    value = "traces.${var.gateway_domain}"
  }

  depends_on = [
    helm_release.ingress_nginx,
    helm_release.recsys_serving,
    helm_release.recsys_observability,
  ]
}
