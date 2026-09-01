output "gateway_basic_auth_password" {
  value     = var.config.gateway_htpasswd == null ? random_password.gateway_basic_auth.result : null
  sensitive = true
}

output "agent_registry_endpoint" {
  value = var.config.deploy_agent_registry ? "http://agentregistry.agentregistry.svc.cluster.local:12121" : null
}

output "vault_endpoint" {
  value = var.config.deploy_vault ? "http://vault.vault.svc.cluster.local:8200" : null
}

output "vault_kms_key" {
  value = var.config.deploy_vault ? google_kms_crypto_key.vault_unseal[0].id : null
}

output "langfuse_url" {
  value = var.config.deploy_langfuse ? "https://${var.config.langfuse_domain}" : null
}

output "langfuse_cloud_sql_instance" {
  value = var.config.deploy_langfuse && var.config.langfuse_backend_mode == "managed" ? google_sql_database_instance.langfuse[0].connection_name : null
}

output "langfuse_gcs_bucket" {
  value = var.config.deploy_langfuse ? google_storage_bucket.langfuse[0].name : null
}

output "langfuse_backend_mode" {
  value = var.config.deploy_langfuse ? var.config.langfuse_backend_mode : null
}
