locals {
  helm_dir      = "${path.module}/../../helm"
  bucket_prefix = replace(lower("${var.project_id}-${var.name_prefix}"), "_", "-")
  image_repo    = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repository}"

  images = {
    dataflow_cli        = lookup(var.image_overrides, "dataflow_cli", "") != "" ? lookup(var.image_overrides, "dataflow_cli", "") : "${local.image_repo}/recsys-dataflow-cli:${var.image_tag}"
    spark               = lookup(var.image_overrides, "spark", "") != "" ? lookup(var.image_overrides, "spark", "") : "${local.image_repo}/recsys-spark:${var.image_tag}"
    flink               = lookup(var.image_overrides, "flink", "") != "" ? lookup(var.image_overrides, "flink", "") : "${local.image_repo}/recsys-flink:${var.image_tag}"
    kafka_connect       = lookup(var.image_overrides, "kafka_connect", "") != "" ? lookup(var.image_overrides, "kafka_connect", "") : "${local.image_repo}/recsys-kafka-connect:${var.image_tag}"
    airflow             = lookup(var.image_overrides, "airflow", "") != "" ? lookup(var.image_overrides, "airflow", "") : "${local.image_repo}/recsys-airflow:${var.image_tag}"
    mlflow              = lookup(var.image_overrides, "mlflow", "") != "" ? lookup(var.image_overrides, "mlflow", "") : "${local.image_repo}/recsys-mlflow:${var.image_tag}"
    api                 = lookup(var.image_overrides, "api", "") != "" ? lookup(var.image_overrides, "api", "") : "${local.image_repo}/recsys-api-serving:${var.image_tag}"
    training_repository = lookup(var.image_overrides, "training_repository", "") != "" ? lookup(var.image_overrides, "training_repository", "") : "${local.image_repo}/recsys-mlops-training"
  }

  data_platform_sets = {
    "images.dataflowCli"   = local.images.dataflow_cli
    "images.spark"         = local.images.spark
    "images.flink"         = local.images.flink
    "images.kafkaConnect"  = local.images.kafka_connect
    "images.airflow"       = local.images.airflow
    "secret.create"        = "true"
    "minio.rootUser"       = "minio"
    "sourcePostgres.user"  = "recsys"
    "airflowPostgres.user" = "airflow"
  }

  mlflow_sets = {
    "mlflow.image"      = local.images.mlflow
    "secret.create"     = "true"
    "minio.rootUser"    = "minio"
    "postgres.user"     = "mlflow"
    "postgres.database" = "mlflow"
  }

  runtime_sets = {
    "secret.create"        = "true"
    "secret.name"          = "recsys-mlops-runtime"
    "secret.minioRootUser" = "minio"
  }

  serving_sets = {
    "api.image"                                                = local.images.api
    "kserve.secret.create"                                     = "true"
    "kserve.secret.accessKeyId"                                = "minio"
    "kserve.secret.minioEndpoint"                              = "minio.experiment-tracking.svc.cluster.local:9000"
    "kserve.nodeSelector.cloud\\.google\\.com/gke-accelerator" = var.gpu_accelerator_type
    "observability.serviceMonitor.enabled"                     = "false"
  }

  ray_sets = {
    "image.repository"                                      = local.images.training_repository
    "image.tag"                                             = var.image_tag
    "gpu.nodeSelector.cloud\\.google\\.com/gke-accelerator" = var.gpu_accelerator_type
  }
}
