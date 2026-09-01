output "cluster_id" {
  value = google_container_cluster.recsys.id
}

output "cluster_name" {
  value = google_container_cluster.recsys.name
}

output "cluster_location" {
  value = google_container_cluster.recsys.location
}

output "cluster_endpoint" {
  value     = google_container_cluster.recsys.endpoint
  sensitive = true
}

output "cluster_ca_certificate" {
  value     = google_container_cluster.recsys.master_auth[0].cluster_ca_certificate
  sensitive = true
}

output "cpu_node_pool_name" {
  value = google_container_node_pool.cpu.name
}

output "langfuse_node_pool_name" {
  value = var.config.deploy_langfuse && var.config.langfuse_backend_mode == "managed" ? google_container_node_pool.langfuse[0].name : null
}

output "gpu_pool_summary" {
  value = var.config.enable_gpu_pool ? {
    name             = google_container_node_pool.gpu[0].name
    machine_type     = var.config.gpu_machine_type
    accelerator_type = var.config.gpu_accelerator_type
    min_nodes        = var.config.gpu_min_nodes
    max_nodes        = var.config.gpu_max_nodes
    spot             = var.config.gpu_spot
  } : null
}

output "llm_cpu_pool_summary" {
  value = var.config.deploy_llm_inference && var.config.llm_node_pool_mode == "dedicated" ? {
    name         = google_container_node_pool.llm_cpu[0].name
    machine_type = var.config.llm_cpu_machine_type
    min_nodes    = var.config.llm_cpu_min_nodes
    max_nodes    = var.config.llm_cpu_max_nodes
    spot         = var.config.llm_cpu_spot
  } : null
}

output "llm_inference_placement" {
  value = var.config.deploy_llm_inference ? {
    mode      = var.config.llm_node_pool_mode
    node_pool = var.config.llm_node_pool_mode == "dedicated" ? google_container_node_pool.llm_cpu[0].name : google_container_node_pool.cpu.name
  } : null
}
