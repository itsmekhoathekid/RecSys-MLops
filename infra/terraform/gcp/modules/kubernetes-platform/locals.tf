locals {
  helm_dir      = var.helm_dir
  bucket_prefix = replace(lower("${var.config.project_id}-${var.config.name_prefix}"), "_", "-")
  image_repo    = "${var.config.region}-docker.pkg.dev/${var.config.project_id}/${var.config.artifact_registry_repository}"

  images = {
    data_ingestion      = lookup(var.config.image_overrides, "data_ingestion", "") != "" ? lookup(var.config.image_overrides, "data_ingestion", "") : "${local.image_repo}/recsys-data-ingestion:${var.config.image_tag}"
    feature_store       = lookup(var.config.image_overrides, "feature_store", "") != "" ? lookup(var.config.image_overrides, "feature_store", "") : "${local.image_repo}/recsys-feature-store:${var.config.image_tag}"
    drift_retrain       = lookup(var.config.image_overrides, "drift_retrain", "") != "" ? lookup(var.config.image_overrides, "drift_retrain", "") : "${local.image_repo}/recsys-drift-retrain:${var.config.image_tag}"
    spark               = lookup(var.config.image_overrides, "spark", "") != "" ? lookup(var.config.image_overrides, "spark", "") : "${local.image_repo}/recsys-spark:${var.config.image_tag}"
    flink               = lookup(var.config.image_overrides, "flink", "") != "" ? lookup(var.config.image_overrides, "flink", "") : "${local.image_repo}/recsys-flink:${var.config.image_tag}"
    kafka_connect       = lookup(var.config.image_overrides, "kafka_connect", "") != "" ? lookup(var.config.image_overrides, "kafka_connect", "") : "${local.image_repo}/recsys-kafka-connect:${var.config.image_tag}"
    airflow             = lookup(var.config.image_overrides, "airflow", "") != "" ? lookup(var.config.image_overrides, "airflow", "") : "${local.image_repo}/recsys-airflow:${var.config.image_tag}"
    analytics_dbt       = lookup(var.config.image_overrides, "analytics_dbt", "") != "" ? lookup(var.config.image_overrides, "analytics_dbt", "") : "${local.image_repo}/recsys-analytics-dbt:${var.config.image_tag}"
    mlflow              = lookup(var.config.image_overrides, "mlflow", "") != "" ? lookup(var.config.image_overrides, "mlflow", "") : "${local.image_repo}/recsys-mlflow:${var.config.image_tag}"
    online_feature_api  = lookup(var.config.image_overrides, "online_feature_api", "") != "" ? lookup(var.config.image_overrides, "online_feature_api", "") : "${local.image_repo}/recsys-online-feature-api:${var.config.image_tag}"
    inference_api       = lookup(var.config.image_overrides, "inference_api", "") != "" ? lookup(var.config.image_overrides, "inference_api", "") : "${local.image_repo}/recsys-inference-api:${var.config.image_tag}"
    training_repository = lookup(var.config.image_overrides, "training_repository", "") != "" ? lookup(var.config.image_overrides, "training_repository", "") : "${local.image_repo}/recsys-mlops-training"
  }

  data_config_sets = {
    "images.dataIngestion" = local.images.data_ingestion
    "images.featureStore"  = local.images.feature_store
    "images.driftRetrain"  = local.images.drift_retrain
    "images.spark"         = local.images.spark
    "images.flink"         = local.images.flink
    "images.analyticsDbt"  = local.images.analytics_dbt
    "secret.create"        = "false"
    "minio.rootUser"       = "minio"
    "sourcePostgres.user"  = "recsys"
    "airflowPostgres.user" = "airflow"
  }

  mlflow_sets = {
    "namespace.create"  = "false"
    "mlflow.image"      = local.images.mlflow
    "secret.create"     = "false"
    "minio.rootUser"    = "minio"
    "postgres.user"     = "mlflow"
    "postgres.database" = "mlflow"
  }

  ml_system_sets = {
    "nodeSelector.recsys\\.ai/workload" = "ml-system"
    "tolerations[0].key"                = "recsys.ai/workload"
    "tolerations[0].operator"           = "Equal"
    "tolerations[0].value"              = "ml-system"
    "tolerations[0].effect"             = "NoSchedule"
  }

  runtime_sets = {
    "namespace.create"     = "false"
    "secret.create"        = "false"
    "secret.name"          = "recsys-mlops-runtime"
    "secret.minioRootUser" = "minio"
  }

  serving_sets = {
    "kserve.namespace.create"                  = "false"
    "kserve.secret.create"                     = "false"
    "kserve.secret.accessKeyId"                = "minio"
    "kserve.secret.minioEndpoint"              = "minio.experiment-tracking.svc.cluster.local:9000"
    "kserve.nodeSelector.recsys\\.ai/workload" = "ml-system"
    "kserve.tolerations[0].key"                = "recsys.ai/workload"
    "kserve.tolerations[0].operator"           = "Equal"
    "kserve.tolerations[0].value"              = "ml-system"
    "kserve.tolerations[0].effect"             = "NoSchedule"
  }

  online_feature_api_sets = merge(local.ml_system_sets, {
    image                          = local.images.online_feature_api
    "config.feastPostgresUser"     = "feast"
    "config.feastPostgresPassword" = random_password.feast_postgres.result
  })

  inference_api_sets = merge(local.ml_system_sets, {
    image = local.images.inference_api
  })

  service_mesh_namespaces = [
    "kubeflow",
    "experiment-tracking",
    "recsys-dataflow",
    "kserve-triton-inference",
    "api-serving",
    "observability",
  ]

  external_secrets_chart_revision = sha1(join("", [
    for path in ["externalsecrets.yaml", "secretstore.yaml"] :
    filemd5("${local.helm_dir}/recsys-security/templates/${path}")
  ]))

  # helm_release does not reliably notice changes to non-template chart files
  # such as the probe script or provisioned dashboard JSON. Hash the complete
  # local chart so every reviewed artifact change results in a Helm revision.
  observability_chart_revision = sha1(join("", [
    for path in sort(fileset("${local.helm_dir}/recsys-observability", "**")) :
    filemd5("${local.helm_dir}/recsys-observability/${path}")
  ]))

  gateway_chart_revision = sha1(join("", [
    for path in sort(fileset("${local.helm_dir}/recsys-gateway", "**")) :
    filemd5("${local.helm_dir}/recsys-gateway/${path}")
  ]))

  service_mesh_sets = merge(
    {
      "chartRevision"                                        = sha1("${local.external_secrets_chart_revision}:${filemd5("${local.helm_dir}/recsys-security/templates/istio-authorization.yaml")}")
      "secretStore.enabled"                                  = "true"
      "secretStore.provider"                                 = var.config.deploy_vault ? "vault" : "kubernetes"
      "secretStore.name"                                     = var.config.deploy_vault ? "recsys-vault" : "recsys-central-secrets"
      "secretStore.kubernetes.remoteNamespace"               = "external-secrets"
      "secretStore.kubernetes.auth.serviceAccount.name"      = "external-secrets"
      "secretStore.kubernetes.auth.serviceAccount.namespace" = "external-secrets"
      "vault.server"                                         = "http://vault.vault.svc.cluster.local:8200"
      "vault.mountPath"                                      = "recsys"
      "vault.auth.mountPath"                                 = "kubernetes"
      "vault.auth.role"                                      = "recsys-external-secrets"
      "vault.auth.serviceAccount.name"                       = "external-secrets"
      "vault.auth.serviceAccount.namespace"                  = "external-secrets"
      "externalSecrets.enabled"                              = "true"
      "externalSecrets.creationPolicy"                       = "Owner"
      "externalSecrets.agentGatewayClient.enabled"           = tostring(var.config.deploy_llm_inference && var.config.agent_gateway_auth_enabled)
      "externalSecrets.agentGatewayServer.enabled"           = tostring(var.config.deploy_llm_inference && var.config.agent_gateway_auth_enabled)
      "externalSecrets.agentGatewayProbe.enabled"            = tostring(var.config.deploy_llm_inference && var.config.agent_gateway_auth_enabled)
      "externalSecrets.agentRegistry.enabled"                = tostring(var.config.deploy_agent_registry)
      "externalSecrets.featureRagMcp.enabled"                = tostring(var.config.deploy_llm_inference)
      "externalSecrets.recommendationMcp.enabled"            = tostring(var.config.deploy_llm_inference)
      "externalSecrets.gatewayLangfuse.enabled"              = tostring(var.config.deploy_langfuse)
      "externalSecrets.runtime.additionalVaultPaths[0]"      = "jenkins-runtime"
      "istio.enabled"                                        = tostring(var.config.deploy_service_mesh)
    },
    {
      for index, namespace in local.service_mesh_namespaces :
      "istio.namespaces[${index}]" => namespace
    }
  )

  ray_sets = merge(
    {
      "image.repository" = local.images.training_repository
      "image.tag"        = var.config.image_tag
    },
    var.config.enable_gpu_pool ? {
      "gpu.nodeSelector.cloud\\.google\\.com/gke-accelerator" = var.config.gpu_accelerator_type
      } : {
      "head.nodeSelector.recsys\\.ai/pool"   = "ml-system"
      "head.tolerations[0].key"              = "recsys.ai/workload"
      "head.tolerations[0].operator"         = "Equal"
      "head.tolerations[0].value"            = "ml-system"
      "head.tolerations[0].effect"           = "NoSchedule"
      "worker.nodeSelector.recsys\\.ai/pool" = "ml-system"
      "worker.tolerations[0].key"            = "recsys.ai/workload"
      "worker.tolerations[0].operator"       = "Equal"
      "worker.tolerations[0].value"          = "ml-system"
      "worker.tolerations[0].effect"         = "NoSchedule"
    }
  )
}
