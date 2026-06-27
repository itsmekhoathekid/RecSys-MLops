resource "google_artifact_registry_repository" "docker" {
  location      = var.region
  repository_id = var.artifact_registry_repository
  description   = "RecSys MLOps Docker images"
  format        = "DOCKER"
  labels        = var.labels

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket" "lake_backup" {
  name                        = "${local.bucket_prefix}-lake-backup"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
  labels                      = var.labels

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
  name                        = "${local.bucket_prefix}-model-backup"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
  labels                      = var.labels

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 60
    }
  }
}
