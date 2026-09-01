output "cluster_name" {
  value = module.gke.cluster_name
}

output "cluster_location" {
  value = module.gke.cluster_location
}

output "artifact_registry_repository" {
  value = module.artifact_storage.artifact_registry_repository
}

output "image_repository_prefix" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repository}"
}

output "lake_backup_bucket" {
  value = module.artifact_storage.lake_backup_bucket
}

output "model_backup_bucket" {
  value = module.artifact_storage.model_backup_bucket
}

output "kubectl_get_credentials_command" {
  value = "gcloud container clusters get-credentials ${module.gke.cluster_name} --zone ${var.zone} --project ${var.project_id}"
}

output "gpu_pool_summary" {
  value = module.gke.gpu_pool_summary
}

output "ml_compute_mode" {
  description = "Selected compute mode for Ray training and KServe/Triton serving."
  value       = var.enable_gpu_pool ? "gpu" : "cpu"
}

output "gateway_basic_auth_password" {
  description = "Generated password for the recsys gateway user. Null when gateway_htpasswd is supplied explicitly."
  value       = module.kubernetes_platform.gateway_basic_auth_password
  sensitive   = true
}

output "llm_cpu_pool_summary" {
  value = module.gke.llm_cpu_pool_summary
}

output "llm_inference_placement" {
  value = module.gke.llm_inference_placement
}

output "agent_registry_endpoint" {
  description = "Cluster-internal Agent Registry UI/API endpoint."
  value       = module.kubernetes_platform.agent_registry_endpoint
}

output "vault_endpoint" {
  description = "Cluster-internal HashiCorp Vault endpoint used by External Secrets Operator."
  value       = module.kubernetes_platform.vault_endpoint
}

output "vault_kms_key" {
  description = "Cloud KMS key used for Vault auto-unseal and bootstrap artifact encryption."
  value       = module.kubernetes_platform.vault_kms_key
}

output "langfuse_url" {
  description = "Public Langfuse production URL protected by ingress Basic Auth and Langfuse authentication."
  value       = module.kubernetes_platform.langfuse_url
}

output "langfuse_cloud_sql_instance" {
  description = "Cloud SQL connection name for the private HA Langfuse PostgreSQL instance."
  value       = module.kubernetes_platform.langfuse_cloud_sql_instance
}

output "langfuse_gcs_bucket" {
  description = "Private GCS bucket used for Langfuse events, exports, and media."
  value       = module.kubernetes_platform.langfuse_gcs_bucket
}

output "langfuse_backend_mode" {
  description = "Active Langfuse data-plane profile."
  value       = module.kubernetes_platform.langfuse_backend_mode
}
