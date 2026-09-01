resource "google_artifact_registry_repository" "docker" {
  location      = var.config.region
  repository_id = var.config.artifact_registry_repository
  description   = "RecSys MLOps Docker images"
  format        = "DOCKER"
  labels        = var.config.labels

  lifecycle {
    precondition {
      condition     = length(var.api_service_ids) > 0
      error_message = "Required Google APIs must be enabled before creating Artifact Registry."
    }
  }

}

resource "google_storage_bucket" "lake_backup" {
  name                        = "${replace(lower("${var.config.project_id}-${var.config.name_prefix}"), "_", "-")}-lake-backup"
  location                    = var.config.region
  uniform_bucket_level_access = true
  force_destroy               = false
  labels                      = var.config.labels

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 30
    }
  }
}

resource "google_storage_bucket" "model_backup" {
  name                        = "${replace(lower("${var.config.project_id}-${var.config.name_prefix}"), "_", "-")}-model-backup"
  location                    = var.config.region
  uniform_bucket_level_access = true
  force_destroy               = false
  labels                      = var.config.labels

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 60
    }
  }
}
