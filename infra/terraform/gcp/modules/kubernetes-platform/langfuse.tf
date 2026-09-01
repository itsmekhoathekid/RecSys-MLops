locals {
  langfuse_bucket_name = "${var.config.project_id}-langfuse"
  langfuse_managed_backend = (
    var.config.deploy_langfuse && var.config.langfuse_backend_mode == "managed"
  )
  langfuse_in_cluster_backend = (
    var.config.deploy_langfuse && var.config.langfuse_backend_mode == "in_cluster"
  )
  langfuse_labels = merge(var.config.labels, {
    component = "langfuse"
    managed   = "terraform"
  })
}

resource "null_resource" "langfuse_private_service_access" {
  count = local.langfuse_managed_backend ? 1 : 0

  triggers = {
    connection_id = var.cluster.private_service_connection_id
    services_hash = sha256(join(",", sort(var.api_service_ids)))
  }
}

resource "kubernetes_storage_class_v1" "langfuse_hdd" {
  count = var.config.deploy_langfuse ? 1 : 0

  metadata {
    name = "langfuse-hdd-rwo"
    labels = {
      "app.kubernetes.io/part-of" = "recsys-mlops"
      "recsys.ai/storage-scope"   = "langfuse"
    }
  }

  storage_provisioner    = "pd.csi.storage.gke.io"
  reclaim_policy         = "Delete"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true

  parameters = {
    type = "pd-standard"
  }
}

resource "random_password" "langfuse_postgres" {
  count = local.langfuse_managed_backend ? 1 : 0

  length  = 40
  special = false
}

resource "random_password" "langfuse_clickhouse" {
  count = var.config.deploy_langfuse ? 1 : 0

  length  = 40
  special = false
}

resource "random_password" "langfuse_admin" {
  count = var.config.deploy_langfuse ? 1 : 0

  length           = 32
  special          = true
  override_special = "!#%+-_"
}

resource "random_password" "langfuse_salt" {
  count = var.config.deploy_langfuse ? 1 : 0

  length  = 64
  special = false
}

resource "random_password" "langfuse_nextauth" {
  count = var.config.deploy_langfuse ? 1 : 0

  length  = 64
  special = false
}

resource "random_password" "langfuse_encryption" {
  count = var.config.deploy_langfuse ? 1 : 0

  length  = 64
  special = false
}

resource "random_id" "langfuse_project_public" {
  count       = var.config.deploy_langfuse ? 1 : 0
  byte_length = 20
}

resource "random_password" "langfuse_project_secret" {
  count = var.config.deploy_langfuse ? 1 : 0

  length  = 64
  special = false
}

resource "google_sql_database_instance" "langfuse" {
  count = local.langfuse_managed_backend ? 1 : 0

  name                = "${var.config.name_prefix}-langfuse-pg"
  database_version    = "POSTGRES_16"
  region              = var.config.region
  deletion_protection = var.config.langfuse_managed_backend_deletion_protection

  settings {
    tier              = "db-perf-optimized-N-2"
    edition           = "ENTERPRISE_PLUS"
    availability_type = "REGIONAL"
    # Cloud SQL supports PD_SSD/PD_HDD, not Compute Engine's PD_BALANCED.
    # Enterprise Plus performance-optimized tiers require SSD-backed data.
    disk_type                   = "PD_SSD"
    disk_size                   = 100
    disk_autoresize             = true
    deletion_protection_enabled = var.config.langfuse_managed_backend_deletion_protection
    user_labels                 = local.langfuse_labels

    # Enterprise Plus performance-optimized instances enable this cache.
    # Declare it explicitly so refresh plans preserve the production setting.
    data_cache_config {
      data_cache_enabled = true
    }

    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = var.cluster.network_id
      enable_private_path_for_google_cloud_services = true
      ssl_mode                                      = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "18:00"
      location                       = var.config.region
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 14
      backup_retention_settings {
        # Cloud SQL requires backup retention to exceed transaction-log
        # retention when PITR is enabled.
        retained_backups = 15
        retention_unit   = "COUNT"
      }
    }

    insights_config {
      query_insights_enabled  = true
      query_plans_per_minute  = 5
      query_string_length     = 1024
      record_application_tags = true
      record_client_address   = false
    }

    maintenance_window {
      day          = 7
      hour         = 19
      update_track = "stable"
    }
  }

  depends_on = [null_resource.langfuse_private_service_access]

}

resource "google_sql_database" "langfuse" {
  count = local.langfuse_managed_backend ? 1 : 0

  name            = "langfuse"
  instance        = google_sql_database_instance.langfuse[0].name
  deletion_policy = "ABANDON"
}

resource "google_sql_database" "langfuse_shadow" {
  count = local.langfuse_managed_backend ? 1 : 0

  name            = "langfuse_shadow"
  instance        = google_sql_database_instance.langfuse[0].name
  deletion_policy = "ABANDON"
}

resource "google_sql_user" "langfuse" {
  count = local.langfuse_managed_backend ? 1 : 0

  name            = "langfuse"
  instance        = google_sql_database_instance.langfuse[0].name
  password        = random_password.langfuse_postgres[0].result
  deletion_policy = "ABANDON"
}

resource "google_redis_instance" "langfuse" {
  count = local.langfuse_managed_backend ? 1 : 0

  name                    = "${var.config.name_prefix}-langfuse-redis"
  tier                    = "STANDARD_HA"
  memory_size_gb          = 1
  region                  = var.config.region
  redis_version           = "REDIS_7_2"
  authorized_network      = var.cluster.network_id
  connect_mode            = "PRIVATE_SERVICE_ACCESS"
  auth_enabled            = true
  transit_encryption_mode = "SERVER_AUTHENTICATION"
  redis_configs = {
    maxmemory-policy = "noeviction"
  }
  labels = local.langfuse_labels

  maintenance_policy {
    weekly_maintenance_window {
      day = "SUNDAY"
      start_time {
        hours   = 19
        minutes = 0
        seconds = 0
        nanos   = 0
      }
    }
  }

  depends_on = [null_resource.langfuse_private_service_access]

}

resource "google_storage_bucket" "langfuse" {
  count = var.config.deploy_langfuse ? 1 : 0

  name                        = local.langfuse_bucket_name
  location                    = var.config.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.langfuse_labels

  versioning {
    enabled = true
  }

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age        = 37
      with_state = "LIVE"
    }
  }

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      days_since_noncurrent_time = 7
      with_state                 = "ARCHIVED"
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_storage_bucket_object" "langfuse_prefixes" {
  for_each = var.config.deploy_langfuse ? toset(["events/", "exports/", "media/"]) : toset([])

  name = each.value
  # Provider 5.x treats an empty string as unset. A tiny marker body keeps the
  # three GCS prefixes materialized without carrying application data.
  content = "managed-by-terraform\n"
  bucket  = google_storage_bucket.langfuse[0].name
}

resource "google_service_account" "langfuse" {
  count = var.config.deploy_langfuse ? 1 : 0

  account_id   = "${var.config.name_prefix}-langfuse"
  display_name = "RecSys Langfuse GCS Workload Identity"
}

resource "google_storage_bucket_iam_member" "langfuse" {
  count = var.config.deploy_langfuse ? 1 : 0

  bucket = google_storage_bucket.langfuse[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.langfuse[0].email}"
}

resource "google_service_account_iam_member" "langfuse_workload_identity" {
  count = var.config.deploy_langfuse ? 1 : 0

  service_account_id = google_service_account.langfuse[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.config.project_id}.svc.id.goog[langfuse/langfuse]"
}

resource "kubernetes_secret_v1" "langfuse_runtime" {
  count = var.config.deploy_langfuse ? 1 : 0

  metadata {
    name      = "recsys-langfuse-runtime"
    namespace = kubernetes_namespace.langfuse[0].metadata[0].name
    labels = {
      "app.kubernetes.io/part-of" = "recsys-mlops"
      "recsys.ai/secret-scope"    = "langfuse"
    }
  }

  data = merge({
    "clickhouse-password"    = random_password.langfuse_clickhouse[0].result
    "salt"                   = random_password.langfuse_salt[0].result
    "encryption-key"         = sha256(random_password.langfuse_encryption[0].result)
    "nextauth-secret"        = random_password.langfuse_nextauth[0].result
    "project-public-key"     = "pk-lf-${random_id.langfuse_project_public[0].hex}"
    "project-secret-key"     = "sk-lf-${random_password.langfuse_project_secret[0].result}"
    "initial-admin-password" = random_password.langfuse_admin[0].result
    }, local.langfuse_managed_backend ? {
    "postgres-password"   = random_password.langfuse_postgres[0].result
    "shadow-database-url" = "postgresql://langfuse:${random_password.langfuse_postgres[0].result}@${google_sql_database_instance.langfuse[0].private_ip_address}:5432/langfuse_shadow?sslmode=require&connect_timeout=10"
    "redis-password"      = google_redis_instance.langfuse[0].auth_string
    "redis-ca.pem"        = google_redis_instance.langfuse[0].server_ca_certs[0].cert
  } : {})

  type = "Opaque"

  depends_on = [
    google_sql_database.langfuse,
    google_sql_database.langfuse_shadow,
    google_sql_user.langfuse,
  ]
}

resource "kubernetes_secret_v1" "langfuse_otel" {
  count = var.config.deploy_langfuse ? 1 : 0

  metadata {
    name      = "recsys-langfuse-otel"
    namespace = kubernetes_namespace.observability.metadata[0].name
    labels = {
      "app.kubernetes.io/part-of" = "recsys-mlops"
      "recsys.ai/secret-scope"    = "langfuse-exporter"
    }
  }

  data = {
    authorization = "Basic ${base64encode("pk-lf-${random_id.langfuse_project_public[0].hex}:sk-lf-${random_password.langfuse_project_secret[0].result}")}"
  }

  type = "Opaque"
}

resource "helm_release" "clickhouse_operator" {
  count = var.config.deploy_langfuse ? 1 : 0

  name       = "clickhouse-operator"
  repository = "oci://ghcr.io/clickhouse"
  chart      = "clickhouse-operator-helm"
  version    = "0.0.5"
  namespace  = kubernetes_namespace.langfuse[0].metadata[0].name
  wait       = true
  atomic     = true
  timeout    = 900

  values = [
    yamlencode({
      controller = {
        watchNamespaces = ["langfuse"]
      }
      manager = {
        replicas = local.langfuse_in_cluster_backend ? 1 : 2
        image = {
          tag    = "v0.0.5"
          digest = "sha256:e9be3bb61f14e04526474e36b9dbf5cbd5760b6d42c805a35f90af28ddb9ffc4"
        }
        nodeSelector = {
          "recsys.ai/pool" = local.langfuse_in_cluster_backend ? "cpu-services" : "langfuse"
        }
        tolerations = local.langfuse_in_cluster_backend ? [] : [{
          key      = "recsys.ai/workload"
          operator = "Equal"
          value    = "langfuse"
          effect   = "NoSchedule"
        }]
        resources = {
          requests = {
            cpu    = "100m"
            memory = "256Mi"
          }
          limits = {
            cpu    = "500m"
            memory = "512Mi"
          }
        }
        topologySpreadConstraints = local.langfuse_in_cluster_backend ? [] : [{
          maxSkew           = 1
          topologyKey       = "kubernetes.io/hostname"
          whenUnsatisfiable = "DoNotSchedule"
          labelSelector = {
            matchLabels = {
              "app.kubernetes.io/name" = "clickhouse-operator-helm"
            }
          }
        }]
      }
      rbac = {
        namespaced = true
      }
      crd = {
        enable = true
        keep   = true
      }
      certManager = {
        enable = true
      }
      metrics = {
        enable = true
        secure = false
      }
      prometheus = {
        scraping_annotations = true
        enable               = false
      }
    })
  ]

  depends_on = [
    kubernetes_namespace.langfuse,
    kubernetes_storage_class_v1.langfuse_hdd,
    helm_release.cert_manager,
  ]
}

resource "helm_release" "langfuse" {
  count = var.config.deploy_langfuse ? 1 : 0

  name       = "langfuse"
  repository = "oci://ghcr.io/langfuse/langfuse-k8s/charts"
  chart      = "langfuse"
  version    = var.config.langfuse_chart_version
  namespace  = kubernetes_namespace.langfuse[0].metadata[0].name
  wait       = true
  atomic     = true
  timeout    = 1800

  values = concat(
    [file("${var.repo_root}/configs/langfuse/values-gcp.yaml")],
    local.langfuse_in_cluster_backend ? [file("${var.repo_root}/configs/langfuse/values-coursework.yaml")] : [],
  )

  dynamic "set" {
    for_each = local.langfuse_managed_backend ? {
      "postgresql.host" = google_sql_database_instance.langfuse[0].private_ip_address
      "redis.host"      = google_redis_instance.langfuse[0].host
      "redis.port"      = tostring(google_redis_instance.langfuse[0].port)
    } : {}
    content {
      name  = set.key
      value = set.value
    }
  }

  set {
    name  = "s3.bucket"
    value = google_storage_bucket.langfuse[0].name
  }

  set {
    name  = "s3.eventUpload.bucket"
    value = google_storage_bucket.langfuse[0].name
  }

  set {
    name  = "s3.batchExport.bucket"
    value = google_storage_bucket.langfuse[0].name
  }

  set {
    name  = "s3.mediaUpload.bucket"
    value = google_storage_bucket.langfuse[0].name
  }

  set {
    name  = "langfuse.serviceAccount.annotations.iam\\.gke\\.io/gcp-service-account"
    value = google_service_account.langfuse[0].email
  }

  depends_on = [
    google_service_account_iam_member.langfuse_workload_identity,
    google_storage_bucket_iam_member.langfuse,
    helm_release.clickhouse_operator,
    kubernetes_storage_class_v1.langfuse_hdd,
    kubernetes_secret_v1.langfuse_runtime,
    null_resource.recsys_external_secrets_ready,
  ]
}

resource "google_gke_backup_backup_plan" "langfuse" {
  count = local.langfuse_managed_backend ? 1 : 0

  name     = "${var.config.name_prefix}-langfuse-daily"
  cluster  = var.cluster.id
  location = var.config.region

  retention_policy {
    backup_retain_days = 14
  }

  backup_schedule {
    cron_schedule = "30 19 * * *"
  }

  backup_config {
    include_volume_data = true
    include_secrets     = true
    selected_namespaces {
      namespaces = ["langfuse"]
    }
  }

  depends_on = [helm_release.langfuse]
}
