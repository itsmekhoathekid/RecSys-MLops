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
    "chartRevision"        = sha1(join("", [for path in ["configmap.yaml", "airflow.yaml", "realtime-flink-consumer.yaml"] : filemd5("${local.helm_dir}/recsys-data-platform/templates/${path}")]))
    "namespace.create"     = "false"
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
    "namespace.create"  = "false"
    "mlflow.image"      = local.images.mlflow
    "secret.create"     = "true"
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
    "secret.create"        = "true"
    "secret.name"          = "recsys-mlops-runtime"
    "secret.minioRootUser" = "minio"
  }

  serving_sets = {
    "api.namespace.create"                        = "false"
    "api.image"                                   = local.images.api
    "kserve.namespace.create"                     = "false"
    "kserve.secret.create"                        = "true"
    "kserve.secret.accessKeyId"                   = "minio"
    "kserve.secret.minioEndpoint"                 = "minio.experiment-tracking.svc.cluster.local:9000"
    "api.nodeSelector.recsys\\.ai/workload"       = "ml-system"
    "api.tolerations[0].key"                      = "recsys.ai/workload"
    "api.tolerations[0].operator"                 = "Equal"
    "api.tolerations[0].value"                    = "ml-system"
    "api.tolerations[0].effect"                   = "NoSchedule"
    "kserve.nodeSelector.recsys\\.ai/workload"    = "ml-system"
    "kserve.tolerations[0].key"                   = "recsys.ai/workload"
    "kserve.tolerations[0].operator"              = "Equal"
    "kserve.tolerations[0].value"                 = "ml-system"
    "kserve.tolerations[0].effect"                = "NoSchedule"
    "observability.serviceMonitor.enabled"        = "false"
    "abTest.enabled"                              = "true"
    "abTest.experimentId"                         = "bst-stable-vs-candidate-20260630"
    "abTest.candidateWeightPercent"               = "20"
    "abTest.controlModelVersion"                  = "stable-001"
    "abTest.candidateModelVersion"                = "candidate-001"
    "kserve.inferenceService.candidateStorageUri" = "s3://recsys-model-store/triton/bst/latest"
  }

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
      "secretStore.enabled"                                  = "true"
      "secretStore.provider"                                 = "kubernetes"
      "secretStore.name"                                     = "recsys-central-secrets"
      "secretStore.kubernetes.remoteNamespace"               = "external-secrets"
      "secretStore.kubernetes.auth.serviceAccount.name"      = "external-secrets"
      "secretStore.kubernetes.auth.serviceAccount.namespace" = "external-secrets"
      "externalSecrets.enabled"                              = "true"
      "externalSecrets.creationPolicy"                       = "Merge"
      "istio.enabled"                                        = "true"
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
