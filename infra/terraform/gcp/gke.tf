resource "google_service_account" "gke_nodes" {
  account_id   = "${var.name_prefix}-nodes"
  display_name = "RecSys MLOps GKE nodes"

  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "gke_node_roles" {
  for_each = toset([
    "roles/artifactregistry.reader",
    "roles/artifactregistry.writer",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
  ])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_project_iam_member" "jenkins_workload_identity_artifact_registry_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "principal://iam.googleapis.com/projects/${data.google_project.current.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/ci/sa/recsys-jenkins"

  depends_on = [google_project_service.required]
}

resource "google_container_cluster" "recsys" {
  provider = google-beta

  name                     = "${var.name_prefix}-gke"
  location                 = var.zone
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = var.deletion_protection
  network                  = google_compute_network.recsys.id
  subnetwork               = google_compute_subnetwork.gke.id
  logging_service          = "logging.googleapis.com/kubernetes"
  monitoring_service       = "monitoring.googleapis.com/kubernetes"

  release_channel {
    channel = var.release_channel
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = "${var.name_prefix}-pods"
    services_secondary_range_name = "${var.name_prefix}-services"
  }

  addons_config {
    http_load_balancing {
      disabled = false
    }

    horizontal_pod_autoscaling {
      disabled = false
    }

    gce_persistent_disk_csi_driver_config {
      enabled = true
    }
  }

  master_auth {
    client_certificate_config {
      issue_client_certificate = false
    }
  }

  dynamic "master_authorized_networks_config" {
    for_each = length(var.master_authorized_cidr_blocks) > 0 ? [1] : []
    content {
      dynamic "cidr_blocks" {
        for_each = var.master_authorized_cidr_blocks
        content {
          cidr_block   = cidr_blocks.value.cidr_block
          display_name = cidr_blocks.value.display_name
        }
      }
    }
  }

  resource_labels = var.labels

  depends_on = [
    google_project_service.required,
    google_compute_subnetwork.gke,
  ]
}

resource "google_container_node_pool" "cpu" {
  provider = google-beta

  name       = "${var.name_prefix}-cpu"
  location   = var.zone
  cluster    = google_container_cluster.recsys.name
  node_count = var.cpu_min_nodes

  autoscaling {
    min_node_count = var.cpu_min_nodes
    max_node_count = var.cpu_max_nodes
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 0
    max_unavailable = 1
  }

  node_config {
    machine_type    = var.cpu_machine_type
    disk_size_gb    = var.cpu_disk_size_gb
    disk_type       = "pd-balanced"
    image_type      = "COS_CONTAINERD"
    spot            = var.cpu_spot
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = merge(var.labels, {
      "recsys.ai/pool"     = "cpu-services"
      "recsys.ai/workload" = "data-platform"
    })
    tags = ["${var.name_prefix}-cpu"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  depends_on = [google_project_iam_member.gke_node_roles]
}

resource "google_container_node_pool" "ml_system" {
  provider = google-beta

  name       = "${var.name_prefix}-ml-system"
  location   = var.zone
  cluster    = google_container_cluster.recsys.name
  node_count = var.ml_min_nodes

  autoscaling {
    min_node_count = var.ml_min_nodes
    max_node_count = var.ml_max_nodes
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 0
    max_unavailable = 1
  }

  node_config {
    machine_type    = var.ml_machine_type
    disk_size_gb    = var.ml_disk_size_gb
    disk_type       = "pd-balanced"
    image_type      = "COS_CONTAINERD"
    spot            = var.ml_spot
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = merge(var.labels, {
      "recsys.ai/pool"     = "ml-system"
      "recsys.ai/workload" = "ml-system"
    })
    tags = ["${var.name_prefix}-ml-system"]

    taint {
      key    = "recsys.ai/workload"
      value  = "ml-system"
      effect = "NO_SCHEDULE"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  depends_on = [google_project_iam_member.gke_node_roles]
}

resource "google_container_node_pool" "gpu" {
  provider = google-beta

  name       = "${var.name_prefix}-gpu"
  location   = var.zone
  cluster    = google_container_cluster.recsys.name
  node_count = var.gpu_min_nodes

  autoscaling {
    min_node_count = var.gpu_min_nodes
    max_node_count = var.gpu_max_nodes
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = var.gpu_machine_type
    disk_size_gb    = var.gpu_disk_size_gb
    disk_type       = "pd-balanced"
    image_type      = "COS_CONTAINERD"
    spot            = var.gpu_spot
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = merge(var.labels, {
      "recsys.ai/pool" = "gpu-ml"
    })
    tags = ["${var.name_prefix}-gpu"]

    guest_accelerator {
      type  = var.gpu_accelerator_type
      count = var.gpu_accelerator_count

      gpu_driver_installation_config {
        gpu_driver_version = "LATEST"
      }
    }

    taint {
      key    = "nvidia.com/gpu"
      value  = "present"
      effect = "NO_SCHEDULE"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  depends_on = [google_project_iam_member.gke_node_roles]
}
