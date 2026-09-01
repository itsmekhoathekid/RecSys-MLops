output "artifact_registry_repository" {
  value = google_artifact_registry_repository.docker.name
}

output "lake_backup_bucket" {
  value = google_storage_bucket.lake_backup.name
}

output "model_backup_bucket" {
  value = google_storage_bucket.model_backup.name
}
