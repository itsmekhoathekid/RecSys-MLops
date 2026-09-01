resource "kubernetes_secret" "datahub_mysql" {
  count = var.config.deploy_datahub ? 1 : 0

  metadata {
    name      = "mysql-secrets"
    namespace = kubernetes_namespace.datahub[0].metadata[0].name
  }

  data = {
    mysql-root-password        = coalesce(var.config.datahub_mysql_root_password, random_password.datahub_mysql_root.result)
    mysql-replication-password = coalesce(var.config.datahub_mysql_replication_password, random_password.datahub_mysql_replication.result)
    mysql-password             = coalesce(var.config.datahub_mysql_password, random_password.datahub_mysql.result)
    mysql-cdc-password         = coalesce(var.config.datahub_mysql_cdc_password, random_password.datahub_mysql_cdc.result)
  }

  depends_on = [kubernetes_namespace.datahub]
}

resource "kubernetes_secret" "datahub_encryption" {
  count = var.config.deploy_datahub ? 1 : 0

  metadata {
    name      = "datahub-encryption-secrets"
    namespace = kubernetes_namespace.datahub[0].metadata[0].name

    annotations = {
      "helm.sh/hook"        = "pre-install,pre-upgrade"
      "helm.sh/hook-weight" = "-5"
    }
  }

  data = {
    encryption_key_secret = coalesce(var.config.datahub_encryption_key_secret, random_password.datahub_encryption_key.result)
  }

  depends_on = [kubernetes_namespace.datahub]
}

resource "kubernetes_service_v1" "datahub_kafka_alias" {
  count = var.config.deploy_datahub ? 1 : 0

  metadata {
    name      = "kafka"
    namespace = kubernetes_namespace.datahub[0].metadata[0].name
  }

  spec {
    type          = "ExternalName"
    external_name = "kafka.recsys-dataflow.svc.cluster.local"

    port {
      name        = "broker"
      port        = 29092
      target_port = 29092
    }
  }

  depends_on = [
    kubernetes_namespace.datahub,
    helm_release.recsys_event_stream,
  ]
}

resource "helm_release" "datahub_prerequisites" {
  count = var.config.deploy_datahub ? 1 : 0

  name       = "prerequisites"
  repository = "https://helm.datahubproject.io/"
  chart      = "datahub-prerequisites"
  namespace  = kubernetes_namespace.datahub[0].metadata[0].name
  wait       = true
  timeout    = 1200

  values = [
    file("${local.helm_dir}/datahub-stack/prerequisites-values.yaml"),
  ]

  depends_on = [
    kubernetes_secret.datahub_mysql,
  ]
}

resource "helm_release" "datahub" {
  count = var.config.deploy_datahub ? 1 : 0

  name       = "datahub"
  repository = "https://helm.datahubproject.io/"
  chart      = "datahub"
  namespace  = kubernetes_namespace.datahub[0].metadata[0].name
  wait       = true
  timeout    = 1200

  values = [
    file("${local.helm_dir}/datahub-stack/datahub-values.yaml"),
  ]

  depends_on = [
    helm_release.datahub_prerequisites,
    kubernetes_secret.datahub_encryption,
    kubernetes_service_v1.datahub_kafka_alias,
    helm_release.recsys_event_stream,
  ]
}
