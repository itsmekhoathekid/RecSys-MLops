output "network_id" {
  value = google_compute_network.recsys.id
}

output "subnetwork_id" {
  value = google_compute_subnetwork.gke.id
}

output "private_service_connection_id" {
  value = var.config.deploy_langfuse && (
    var.config.langfuse_backend_mode == "managed" ||
    var.config.capacity_profile == "compact-12vcpu"
  ) ? google_service_networking_connection.private_service_access[0].id : null
}
