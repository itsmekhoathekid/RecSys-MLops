resource "helm_release" "recsys_observability" {
  name             = "recsys-observability"
  chart            = "${local.helm_dir}/recsys-observability"
  namespace        = "observability"
  create_namespace = false
  wait             = true
  timeout          = 900

  values = [
    file("${local.helm_dir}/recsys-observability/values-gcp.yaml"),
  ]

  set {
    name  = "namespace.create"
    value = "false"
  }

  set {
    name  = "promtail.tolerations[0].key"
    value = "recsys.ai/workload"
  }

  set {
    name  = "promtail.tolerations[0].operator"
    value = "Equal"
  }

  set {
    name  = "promtail.tolerations[0].value"
    value = "ml-system"
  }

  set {
    name  = "promtail.tolerations[0].effect"
    value = "NoSchedule"
  }

  depends_on = [
    null_resource.recsys_external_secrets_ready,
    google_container_node_pool.cpu,
    kubernetes_namespace.observability,
    helm_release.prometheus_operator,
  ]
}

resource "helm_release" "recsys_mlflow" {
  name             = "recsys-mlflow"
  chart            = "${local.helm_dir}/mlflow-stack"
  namespace        = "experiment-tracking"
  create_namespace = false
  wait             = true
  timeout          = 900

  values = [
    file("${local.helm_dir}/mlflow-stack/values-gcp.yaml"),
  ]

  dynamic "set" {
    for_each = merge(local.mlflow_sets, local.ml_system_sets)
    content {
      name  = set.key
      value = set.value
    }
  }

  lifecycle {
    ignore_changes = all
  }

  depends_on = [
    null_resource.recsys_external_secrets_ready,
    google_container_node_pool.ml_system,
    kubernetes_namespace.experiment_tracking,
  ]
}

resource "helm_release" "recsys_runtime" {
  name             = "recsys-runtime"
  chart            = "${local.helm_dir}/recsys-runtime"
  namespace        = "kubeflow"
  create_namespace = false
  wait             = false
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

  depends_on = [
    helm_release.recsys_mlflow,
    null_resource.kubeflow_pipelines,
  ]
}

resource "helm_release" "recsys_data_config" {
  name             = "recsys-data-config"
  chart            = "${local.helm_dir}/recsys-data-config"
  namespace        = "recsys-dataflow"
  create_namespace = false
  wait             = true
  timeout          = 600

  values = [
    file("${local.helm_dir}/recsys-data-config/values-gcp.yaml"),
  ]

  dynamic "set" {
    for_each = local.data_config_sets
    content {
      name  = set.key
      value = set.value
    }
  }

  lifecycle {
    ignore_changes = all
  }

  depends_on = [
    null_resource.recsys_external_secrets_ready,
    helm_release.recsys_observability,
    google_container_node_pool.cpu,
    kubernetes_namespace.recsys_dataflow,
  ]
}

resource "helm_release" "recsys_data_lakehouse" {
  name             = "recsys-data-lakehouse"
  chart            = "${local.helm_dir}/recsys-data-lakehouse"
  namespace        = "recsys-dataflow"
  create_namespace = false
  wait             = true
  timeout          = 900
  values           = [file("${local.helm_dir}/recsys-data-lakehouse/values-gcp.yaml")]

  lifecycle {
    ignore_changes = all
  }

  depends_on = [helm_release.recsys_data_config]
}

resource "helm_release" "recsys_source_store" {
  name             = "recsys-source-store"
  chart            = "${local.helm_dir}/recsys-source-store"
  namespace        = "recsys-dataflow"
  create_namespace = false
  wait             = true
  timeout          = 900
  values           = [file("${local.helm_dir}/recsys-source-store/values-gcp.yaml")]

  set {
    name  = "images.dataIngestion"
    value = local.images.data_ingestion
  }

  lifecycle {
    ignore_changes = all
  }

  depends_on = [helm_release.recsys_data_config]
}

resource "helm_release" "recsys_event_stream" {
  name             = "recsys-event-stream"
  chart            = "${local.helm_dir}/recsys-event-stream"
  namespace        = "recsys-dataflow"
  create_namespace = false
  wait             = true
  timeout          = 900

  lifecycle {
    ignore_changes = all
  }

  depends_on = [helm_release.recsys_data_config]
}

resource "helm_release" "recsys_feature_store" {
  name             = "recsys-feature-store"
  chart            = "${local.helm_dir}/recsys-feature-store"
  namespace        = "recsys-dataflow"
  create_namespace = false
  wait             = true
  timeout          = 900
  values           = [file("${local.helm_dir}/recsys-feature-store/values-gcp.yaml")]

  set {
    name  = "images.featureStore"
    value = local.images.feature_store
  }

  lifecycle {
    ignore_changes = all
  }

  depends_on = [
    helm_release.recsys_data_config,
    helm_release.recsys_data_lakehouse,
  ]
}

resource "helm_release" "recsys_kafka_connect" {
  name             = "recsys-kafka-connect"
  chart            = "${local.helm_dir}/recsys-kafka-connect"
  namespace        = "recsys-dataflow"
  create_namespace = false
  wait             = true
  timeout          = 900
  values           = [file("${local.helm_dir}/recsys-kafka-connect/values-gcp.yaml")]

  set {
    name  = "images.kafkaConnect"
    value = local.images.kafka_connect
  }

  set {
    name  = "images.dataIngestion"
    value = local.images.data_ingestion
  }

  lifecycle {
    ignore_changes = all
  }

  depends_on = [
    helm_release.recsys_data_config,
    helm_release.recsys_source_store,
    helm_release.recsys_event_stream,
  ]
}

resource "helm_release" "recsys_streaming" {
  name             = "recsys-streaming"
  chart            = "${local.helm_dir}/recsys-streaming"
  namespace        = "recsys-dataflow"
  create_namespace = false
  wait             = true
  timeout          = 1200
  values           = [file("${local.helm_dir}/recsys-streaming/values-gcp.yaml")]

  set {
    name  = "images.dataIngestion"
    value = local.images.data_ingestion
  }

  set {
    name  = "images.flink"
    value = local.images.flink
  }

  lifecycle {
    ignore_changes = all
  }

  depends_on = [
    helm_release.recsys_data_config,
    helm_release.recsys_data_lakehouse,
    helm_release.recsys_event_stream,
    helm_release.recsys_feature_store,
  ]
}

resource "helm_release" "recsys_airflow" {
  name             = "recsys-airflow"
  chart            = "${local.helm_dir}/recsys-airflow"
  namespace        = "recsys-dataflow"
  create_namespace = false
  wait             = true
  timeout          = 1200
  values           = [file("${local.helm_dir}/recsys-airflow/values-gcp.yaml")]

  set {
    name  = "images.airflow"
    value = local.images.airflow
  }

  set {
    name  = "images.dataIngestion"
    value = local.images.data_ingestion
  }

  set {
    name  = "images.featureStore"
    value = local.images.feature_store
  }

  set {
    name  = "images.driftRetrain"
    value = local.images.drift_retrain
  }

  set {
    name  = "images.spark"
    value = local.images.spark
  }

  set {
    name  = "images.analyticsDbt"
    value = local.images.analytics_dbt
  }

  lifecycle {
    ignore_changes = all
  }

  depends_on = [
    helm_release.recsys_data_config,
    helm_release.recsys_data_lakehouse,
    helm_release.recsys_source_store,
    helm_release.recsys_event_stream,
    helm_release.recsys_feature_store,
  ]
}

resource "helm_release" "recsys_online_feature_api" {
  count = var.deploy_serving ? 1 : 0

  name             = "recsys-online-feature-api"
  chart            = "${local.helm_dir}/recsys-online-feature-api"
  namespace        = "api-serving"
  create_namespace = false
  wait             = true
  timeout          = 600
  values           = [file("${local.helm_dir}/recsys-online-feature-api/values-gcp.yaml")]

  dynamic "set" {
    for_each = local.online_feature_api_sets
    content {
      name  = set.key
      value = set.value
    }
  }

  lifecycle {
    ignore_changes = all
  }

  depends_on = [
    null_resource.recsys_external_secrets_ready,
    helm_release.recsys_feature_store,
    kubernetes_namespace.api_serving,
  ]
}

resource "helm_release" "recsys_inference_api" {
  count = var.deploy_serving ? 1 : 0

  name             = "recsys-inference-api"
  chart            = "${local.helm_dir}/recsys-inference-api"
  namespace        = "api-serving"
  create_namespace = false
  wait             = true
  timeout          = 600
  values           = [file("${local.helm_dir}/recsys-inference-api/values-gcp.yaml")]

  dynamic "set" {
    for_each = local.inference_api_sets
    content {
      name  = set.key
      value = set.value
    }
  }

  lifecycle {
    ignore_changes = all
  }

  depends_on = [
    null_resource.kserve,
    google_container_node_pool.ml_system,
    kubernetes_namespace.api_serving,
  ]
}

resource "helm_release" "recsys_serving" {
  count = var.deploy_model_serving ? 1 : 0

  name             = "recsys-serving"
  chart            = "${local.helm_dir}/recsys-serving"
  namespace        = "kserve-triton-inference"
  create_namespace = false
  wait             = true
  timeout          = 1200

  values = [
    file(var.enable_gpu_pool ? "${local.helm_dir}/recsys-serving/values-gcp-gpu.yaml" : "${local.helm_dir}/recsys-serving/values-gcp-cpu.yaml"),
  ]

  dynamic "set" {
    for_each = local.serving_sets
    content {
      name  = set.key
      value = set.value
    }
  }

  lifecycle {
    ignore_changes = all
  }

  depends_on = [
    null_resource.recsys_external_secrets_ready,
    helm_release.keda_http,
    helm_release.recsys_mlflow,
    helm_release.recsys_airflow,
    helm_release.recsys_feature_store,
    null_resource.kserve,
    google_container_node_pool.ml_system,
    kubernetes_namespace.api_serving,
    kubernetes_namespace.kserve_triton_inference,
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
    file(var.enable_gpu_pool ? "${local.helm_dir}/ray-cluster/values-gcp-gpu.yaml" : "${local.helm_dir}/ray-cluster/values-gcp-cpu.yaml"),
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
    helm_release.recsys_data_config,
    google_container_node_pool.gpu,
  ]
}

resource "helm_release" "recsys_gateway" {
  count = var.deploy_gateway ? 1 : 0

  name             = "recsys-gateway"
  chart            = "${local.helm_dir}/recsys-gateway"
  namespace        = "api-serving"
  create_namespace = false
  wait             = true
  timeout          = 600

  set {
    name  = "gateway.domain"
    value = var.gateway_domain
  }

  set {
    name  = "api.enabled"
    value = "false"
  }

  set {
    name  = "featureApi.host"
    value = "api.${var.gateway_domain}"
  }

  set {
    name  = "featureApi.rootRedirect.enabled"
    value = "true"
  }

  set {
    name  = "featureApi.rootRedirect.path"
    value = "/docs"
  }

  set {
    name  = "featureApi.upstreamHost"
    value = "recsys-online-feature-api.api-serving.svc.cluster.local"
  }

  set {
    name  = "grafana.host"
    value = "metrics.${var.gateway_domain}"
  }

  set {
    name  = "logs.host"
    value = "logs.${var.gateway_domain}"
  }

  set {
    name  = "logs.upstreamHost"
    value = "recsys-loki.observability.svc.cluster.local"
  }

  set {
    name  = "traces.host"
    value = "traces.${var.gateway_domain}"
  }

  set {
    name  = "traces.upstreamHost"
    value = "recsys-tempo.observability.svc.cluster.local"
  }

  set {
    name  = "tls.enabled"
    value = tostring(var.gateway_tls_enabled)
  }

  set {
    name  = "tls.clusterIssuerName"
    value = var.gateway_tls_cluster_issuer
  }

  set {
    name  = "tls.issuer.create"
    value = tostring(var.gateway_tls_issuer_create)
  }

  set {
    name  = "tls.issuer.name"
    value = var.gateway_tls_cluster_issuer
  }

  set {
    name  = "tls.issuer.email"
    value = var.gateway_tls_issuer_email
  }

  set {
    name  = "tls.issuer.server"
    value = var.gateway_tls_issuer_server
  }

  set {
    name  = "tls.issuer.privateKeySecretName"
    value = "${var.gateway_tls_cluster_issuer}-account-key"
  }

  set {
    name  = "auth.createSecret"
    value = "false"
  }

  set_sensitive {
    name  = "auth.htpasswd"
    value = local.gateway_htpasswd
  }

  lifecycle {
    # bcrypt() salts on every evaluation and Helm marks sensitive set values
    # unknown during planning. Keep the deployed credential stable; rotations
    # must be performed explicitly instead of occurring on an unrelated apply.
    ignore_changes = [set_sensitive]
  }

  depends_on = [
    helm_release.ingress_nginx,
    helm_release.recsys_serving,
    helm_release.recsys_observability,
    kubernetes_namespace.api_serving,
  ]
}

resource "helm_release" "recsys_security" {
  count = 1

  name             = "recsys-security"
  chart            = "${local.helm_dir}/recsys-security"
  namespace        = "recsys-security"
  create_namespace = true
  wait             = true
  timeout          = 600

  dynamic "set" {
    for_each = local.service_mesh_sets
    content {
      name  = set.key
      value = set.value
    }
  }

  depends_on = [
    helm_release.external_secrets,
    helm_release.istiod,
    helm_release.vault,
    kubernetes_secret_v1.centralized_recsys,
    null_resource.kubeflow_pipelines,
    kubernetes_namespace.experiment_tracking,
    kubernetes_namespace.recsys_dataflow,
    kubernetes_namespace.observability,
    kubernetes_namespace.api_serving,
    kubernetes_namespace.kserve_triton_inference,
    kubernetes_namespace.kagent,
    kubernetes_namespace.llm_inference,
    kubernetes_namespace.agentregistry,
  ]
}
