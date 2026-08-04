locals {
  helm_dir      = "${path.module}/../../helm"
  bucket_prefix = replace(lower("${var.project_id}-${var.name_prefix}"), "_", "-")
  image_repo    = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repository}"

  images = {
    data_ingestion      = lookup(var.image_overrides, "data_ingestion", "") != "" ? lookup(var.image_overrides, "data_ingestion", "") : "${local.image_repo}/recsys-data-ingestion:${var.image_tag}"
    feature_store       = lookup(var.image_overrides, "feature_store", "") != "" ? lookup(var.image_overrides, "feature_store", "") : "${local.image_repo}/recsys-feature-store:${var.image_tag}"
    drift_retrain       = lookup(var.image_overrides, "drift_retrain", "") != "" ? lookup(var.image_overrides, "drift_retrain", "") : "${local.image_repo}/recsys-drift-retrain:${var.image_tag}"
    spark               = lookup(var.image_overrides, "spark", "") != "" ? lookup(var.image_overrides, "spark", "") : "${local.image_repo}/recsys-spark:${var.image_tag}"
    flink               = lookup(var.image_overrides, "flink", "") != "" ? lookup(var.image_overrides, "flink", "") : "${local.image_repo}/recsys-flink:${var.image_tag}"
    kafka_connect       = lookup(var.image_overrides, "kafka_connect", "") != "" ? lookup(var.image_overrides, "kafka_connect", "") : "${local.image_repo}/recsys-kafka-connect:${var.image_tag}"
    airflow             = lookup(var.image_overrides, "airflow", "") != "" ? lookup(var.image_overrides, "airflow", "") : "${local.image_repo}/recsys-airflow:${var.image_tag}"
    mlflow              = lookup(var.image_overrides, "mlflow", "") != "" ? lookup(var.image_overrides, "mlflow", "") : "${local.image_repo}/recsys-mlflow:${var.image_tag}"
    online_feature_api  = lookup(var.image_overrides, "online_feature_api", "") != "" ? lookup(var.image_overrides, "online_feature_api", "") : "${local.image_repo}/recsys-online-feature-api:${var.image_tag}"
    inference_api       = lookup(var.image_overrides, "inference_api", "") != "" ? lookup(var.image_overrides, "inference_api", "") : "${local.image_repo}/recsys-inference-api:${var.image_tag}"
    training_repository = lookup(var.image_overrides, "training_repository", "") != "" ? lookup(var.image_overrides, "training_repository", "") : "${local.image_repo}/recsys-mlops-training"
  }

  data_config_sets = {
    "images.dataIngestion" = local.images.data_ingestion
    "images.featureStore"  = local.images.feature_store
    "images.driftRetrain"  = local.images.drift_retrain
    "images.spark"         = local.images.spark
    "images.flink"         = local.images.flink
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

  service_mesh_sets = merge(
    {
      "chartRevision"                                        = sha1(join("", [for path in ["externalsecrets.yaml", "secretstore.yaml"] : filemd5("${local.helm_dir}/recsys-security/templates/${path}")]))
      "secretStore.enabled"                                  = "true"
      "secretStore.provider"                                 = "kubernetes"
      "secretStore.name"                                     = "recsys-central-secrets"
      "secretStore.kubernetes.remoteNamespace"               = "external-secrets"
      "secretStore.kubernetes.auth.serviceAccount.name"      = "external-secrets"
      "secretStore.kubernetes.auth.serviceAccount.namespace" = "external-secrets"
      "externalSecrets.enabled"                              = "true"
      "externalSecrets.creationPolicy"                       = "Owner"
      "externalSecrets.runtime.additionalVaultPaths[0]"      = "jenkins-runtime"
      "istio.enabled"                                        = tostring(var.deploy_service_mesh)
    },
    {
      for index, namespace in local.service_mesh_namespaces :
      "istio.namespaces[${index}]" => namespace
    }
  )

  ray_sets = {
    "image.repository"                                      = local.images.training_repository
    "image.tag"                                             = var.image_tag
    "gpu.nodeSelector.cloud\\.google\\.com/gke-accelerator" = var.gpu_accelerator_type
  }
}
