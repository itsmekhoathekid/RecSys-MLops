resource "random_password" "minio_root" {
  length  = 32
  special = false
}

resource "random_password" "source_postgres" {
  length  = 32
  special = false
}

resource "random_password" "airflow_postgres" {
  length  = 32
  special = false
}

resource "random_password" "mlflow_postgres" {
  length  = 32
  special = false
}
