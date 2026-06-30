resource "kubernetes_secret" "datahub_mysql" {
  count = var.deploy_datahub ? 1 : 0

  metadata {
    name      = "mysql-secrets"
    namespace = kubernetes_namespace.datahub[0].metadata[0].name
  }

  data = {
    mysql-root-password        = "datahub"
    mysql-replication-password = "datahub"
    mysql-password             = "datahub"
    mysql-cdc-password         = "datahub"
  }

  depends_on = [kubernetes_namespace.datahub]
}

resource "kubernetes_secret" "datahub_encryption" {
  count = var.deploy_datahub ? 1 : 0

  metadata {
    name      = "datahub-encryption-secrets"
    namespace = kubernetes_namespace.datahub[0].metadata[0].name

    annotations = {
      "helm.sh/hook"        = "pre-install,pre-upgrade"
      "helm.sh/hook-weight" = "-5"
    }
  }

  data = {
    encryption_key_secret = "datahub-encryption-key-local"
  }

  depends_on = [kubernetes_namespace.datahub]
}

resource "kubernetes_service_v1" "datahub_kafka_alias" {
  count = var.deploy_datahub ? 1 : 0

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
    helm_release.recsys_data_platform,
  ]
}

resource "helm_release" "datahub_prerequisites" {
  count = var.deploy_datahub ? 1 : 0

  name       = "prerequisites"
  repository = "https://helm.datahubproject.io/"
  chart      = "datahub-prerequisites"
  namespace  = kubernetes_namespace.datahub[0].metadata[0].name
  wait       = true
  timeout    = 1200

  values = [
    file("${local.helm_dir}/datahub-local/prerequisites-values.yaml"),
  ]

  depends_on = [
    kubernetes_secret.datahub_mysql,
    google_container_node_pool.cpu,
  ]
}

resource "helm_release" "datahub" {
  count = var.deploy_datahub ? 1 : 0

  name       = "datahub"
  repository = "https://helm.datahubproject.io/"
  chart      = "datahub"
  namespace  = kubernetes_namespace.datahub[0].metadata[0].name
  wait       = true
  timeout    = 1200

  values = [
    file("${local.helm_dir}/datahub-local/datahub-values.yaml"),
  ]

  depends_on = [
    helm_release.datahub_prerequisites,
    kubernetes_secret.datahub_encryption,
    kubernetes_service_v1.datahub_kafka_alias,
    helm_release.recsys_data_platform,
  ]
}
