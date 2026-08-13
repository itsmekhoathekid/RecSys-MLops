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

output "llm_cpu_pool_summary" {
  value = var.deploy_llm_inference && var.llm_node_pool_mode == "dedicated" ? {
    name         = google_container_node_pool.llm_cpu[0].name
    machine_type = var.llm_cpu_machine_type
    min_nodes    = var.llm_cpu_min_nodes
    max_nodes    = var.llm_cpu_max_nodes
    spot         = var.llm_cpu_spot
  } : null
}

output "llm_inference_placement" {
  value = var.deploy_llm_inference ? {
    mode      = var.llm_node_pool_mode
    node_pool = var.llm_node_pool_mode == "dedicated" ? google_container_node_pool.llm_cpu[0].name : google_container_node_pool.cpu.name
  } : null
}

output "vault_endpoint" {
  description = "Cluster-internal HashiCorp Vault endpoint used by External Secrets Operator."
  value       = var.deploy_vault ? "http://vault.vault.svc.cluster.local:8200" : null
}

output "vault_kms_key" {
  description = "Cloud KMS key used for Vault auto-unseal and bootstrap artifact encryption."
  value       = var.deploy_vault ? google_kms_crypto_key.vault_unseal[0].id : null
}
