data "google_project" "current" {
  project_id = var.project_id
}

locals {
  cloud_build_legacy_service_account  = "${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
  cloud_build_runtime_service_account = "${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

resource "google_project_iam_member" "cloud_build_artifact_registry_writer" {
  for_each = toset([
    local.cloud_build_legacy_service_account,
    local.cloud_build_runtime_service_account,
  ])

  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${each.value}"

  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "cloud_build_runtime_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${local.cloud_build_runtime_service_account}"

  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "cloud_build_runtime_storage_reader" {
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:${local.cloud_build_runtime_service_account}"

  depends_on = [google_project_service.required]
}
