resource "google_compute_network" "recsys" {
  name                    = "${var.config.name_prefix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  lifecycle {
    precondition {
      condition     = length(var.api_service_ids) > 0
      error_message = "Required Google APIs must be enabled before creating the network."
    }
  }

}

resource "google_compute_subnetwork" "gke" {
  name          = "${var.config.name_prefix}-gke"
  ip_cidr_range = var.config.vpc_cidr
  region        = var.config.region
  network       = google_compute_network.recsys.id

  secondary_ip_range {
    range_name    = "${var.config.name_prefix}-pods"
    ip_cidr_range = var.config.pods_cidr
  }

  secondary_ip_range {
    range_name    = "${var.config.name_prefix}-services"
    ip_cidr_range = var.config.services_cidr
  }
}

resource "google_compute_global_address" "private_service_access" {
  count = var.config.deploy_langfuse && var.config.langfuse_backend_mode == "managed" ? 1 : 0

  name          = "${var.config.name_prefix}-private-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.recsys.id
}

resource "google_service_networking_connection" "private_service_access" {
  count = var.config.deploy_langfuse && var.config.langfuse_backend_mode == "managed" ? 1 : 0

  network                 = google_compute_network.recsys.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_service_access[0].name]

  depends_on = [google_compute_global_address.private_service_access]
}
