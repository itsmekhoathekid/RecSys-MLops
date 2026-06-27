output "cluster_name" {
  value = google_container_cluster.recsys.name
}

output "cluster_location" {
  value = google_container_cluster.recsys.location
}

output "artifact_registry_repository" {
  value = google_artifact_registry_repository.docker.name
}

output "image_repository_prefix" {
  value = local.image_repo
}

output "lake_backup_bucket" {
  value = google_storage_bucket.lake_backup.name
}

output "model_backup_bucket" {
  value = google_storage_bucket.model_backup.name
}

output "kubectl_get_credentials_command" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.recsys.name} --zone ${var.zone} --project ${var.project_id}"
}

output "gpu_pool_summary" {
  value = {
    machine_type     = var.gpu_machine_type
    accelerator_type = var.gpu_accelerator_type
    min_nodes        = var.gpu_min_nodes
    max_nodes        = var.gpu_max_nodes
    spot             = var.gpu_spot
  }
}
