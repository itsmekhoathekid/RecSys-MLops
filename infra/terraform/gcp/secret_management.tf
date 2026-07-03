locals {
  external_secret_source_namespace = "external-secrets"
  gateway_htpasswd                 = coalesce(var.gateway_htpasswd, "recsys:!set-TF_VAR_gateway_htpasswd")

  external_secret_payloads = {
    data-platform = {
      DATA_PLATFORM_MINIO_ROOT_USER     = "minio"
      DATA_PLATFORM_MINIO_ROOT_PASSWORD = random_password.minio_root.result
      MINIO_ROOT_USER                   = "minio"
      MINIO_ROOT_PASSWORD               = random_password.minio_root.result
      AWS_ACCESS_KEY_ID                 = "minio"
      AWS_SECRET_ACCESS_KEY             = random_password.minio_root.result
      POSTGRES_USER                     = "recsys"
      POSTGRES_PASSWORD                 = random_password.source_postgres.result
      AIRFLOW_POSTGRES_USER             = "airflow"
      AIRFLOW_POSTGRES_PASSWORD         = random_password.airflow_postgres.result
      FEAST_POSTGRES_USER               = "feast"
      FEAST_POSTGRES_PASSWORD           = "feast"
    }
    mlflow = {
      MINIO_ROOT_USER     = "minio"
      MINIO_ROOT_PASSWORD = random_password.minio_root.result
      POSTGRES_DB         = "mlflow"
      POSTGRES_USER       = "mlflow"
      POSTGRES_PASSWORD   = random_password.mlflow_postgres.result
    }
    runtime = {
      MINIO_ENDPOINT              = "http://data-platform-minio.recsys-dataflow.svc.cluster.local:9000"
      MINIO_ROOT_USER             = "minio"
      MINIO_ROOT_PASSWORD         = random_password.minio_root.result
      AWS_ACCESS_KEY_ID           = "minio"
      AWS_SECRET_ACCESS_KEY       = random_password.minio_root.result
      AWS_DEFAULT_REGION          = "us-east-1"
      MLFLOW_S3_ENDPOINT_URL      = "http://minio.experiment-tracking.svc.cluster.local:9000"
      MODEL_STORE_ENDPOINT        = "http://minio.experiment-tracking.svc.cluster.local:9000"
      MLFLOW_TRACKING_URI         = "http://mlflow.experiment-tracking.svc.cluster.local:5000"
      MLFLOW_EXPERIMENT_NAME      = "recsys-bst-ranking"
      MODEL_REGISTRY_POSTGRES_URI = "postgresql://mlflow:${random_password.mlflow_postgres.result}@postgres.experiment-tracking.svc.cluster.local:5432/mlflow"
      MODEL_STORE_BUCKET          = "recsys-model-store"
      MODEL_STORE_PREFIX          = "triton/bst"
      PROMOTION_MANIFEST_KEY      = "promotions/bst/latest.json"
      ICEBERG_ENABLED             = "true"
      ICEBERG_CATALOG_NAME        = "recsys_features"
      ICEBERG_WAREHOUSE           = "s3a://recsys-offline-feature-store/warehouse"
      HUDI_ENABLED                = "true"
      HUDI_CATALOG_NAME           = "recsys_features"
      HUDI_WAREHOUSE              = "s3a://recsys-offline-feature-store/warehouse"
    }
    kserve-minio = {
      AWS_ACCESS_KEY_ID     = "minio"
      AWS_SECRET_ACCESS_KEY = random_password.minio_root.result
      AWS_DEFAULT_REGION    = "us-east-1"
      AWS_ENDPOINT_URL      = "http://minio.experiment-tracking.svc.cluster.local:9000"
      S3_ENDPOINT           = "minio.experiment-tracking.svc.cluster.local:9000"
      S3_USE_HTTPS          = "0"
    }
    gateway = {
      auth = local.gateway_htpasswd
    }
  }
}

resource "kubernetes_secret_v1" "centralized_recsys" {
  for_each = var.deploy_service_mesh ? local.external_secret_payloads : {}

  metadata {
    name      = each.key
    namespace = local.external_secret_source_namespace
    labels = {
      "app.kubernetes.io/part-of" = "recsys-mlops"
      "recsys.ai/secret-scope"    = each.key
    }
  }

  data = each.value
  type = "Opaque"

  depends_on = [helm_release.external_secrets]
}
