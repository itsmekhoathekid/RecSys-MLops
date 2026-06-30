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

resource "random_password" "datahub_mysql_root" {
  length  = 32
  special = false
}

resource "random_password" "datahub_mysql_replication" {
  length  = 32
  special = false
}

resource "random_password" "datahub_mysql" {
  length  = 32
  special = false
}

resource "random_password" "datahub_mysql_cdc" {
  length  = 32
  special = false
}

resource "random_password" "datahub_encryption_key" {
  length  = 48
  special = false
}
