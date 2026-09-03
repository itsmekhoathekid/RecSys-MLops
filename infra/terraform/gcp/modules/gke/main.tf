resource "google_service_account" "gke_nodes" {
  account_id   = "${var.config.name_prefix}-nodes"
  display_name = "RecSys MLOps GKE nodes"

  lifecycle {
    precondition {
      condition     = length(var.api_service_ids) > 0
      error_message = "Required Google APIs must be enabled before creating GKE resources."
    }
  }

}

resource "google_project_iam_member" "gke_node_roles" {
  for_each = toset([
    "roles/artifactregistry.reader",
    "roles/artifactregistry.writer",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
  ])

  project = var.config.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_project_iam_member" "jenkins_workload_identity_artifact_registry_writer" {
  project = var.config.project_id
  role    = "roles/artifactregistry.writer"
  member  = "principal://iam.googleapis.com/projects/${var.project_number}/locations/global/workloadIdentityPools/${var.config.project_id}.svc.id.goog/subject/ns/ci/sa/recsys-jenkins"

  # The principal namespace is only materialized after GKE creates the
  # project's managed Workload Identity pool. Depending on the API alone can
  # race the cluster creation on a fresh project and return "Identity Pool does
  # not exist" from IAM.
  depends_on = [google_container_cluster.recsys]
}

resource "google_project_iam_member" "atelet_workload_identity_artifact_registry_reader" {
  count = var.config.deploy_llm_inference ? 1 : 0

  project = var.config.project_id
  role    = "roles/artifactregistry.reader"
  member  = "principal://iam.googleapis.com/projects/${var.project_number}/locations/global/workloadIdentityPools/${var.config.project_id}.svc.id.goog/subject/ns/ate-system/sa/atelet"

  # When image-pull authentication is enabled, atelet exchanges its Kubernetes
  # identity directly through Workload Identity instead of using the node SA.
  depends_on = [google_container_cluster.recsys]
}

resource "google_container_cluster" "recsys" {
  provider = google-beta

  name                     = "${var.config.name_prefix}-gke"
  location                 = var.config.zone
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = var.config.deletion_protection
  network                  = var.network_id
  subnetwork               = var.subnetwork_id
  logging_service          = "logging.googleapis.com/kubernetes"
  monitoring_service       = "monitoring.googleapis.com/kubernetes"

  # Grafana reads the in-cluster Prometheus instance, so keep only GKE's
  # no-cost system metrics in Cloud Monitoring. Managed Prometheus previously
  # duplicated collection and ingested tens of millions of billable samples
  # per day without serving the Grafana dashboards.
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]

    managed_prometheus {
      enabled = false
    }
  }

  release_channel {
    channel = var.config.release_channel
  }

  workload_identity_config {
    workload_pool = "${var.config.project_id}.svc.id.goog"
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = "${var.config.name_prefix}-pods"
    services_secondary_range_name = "${var.config.name_prefix}-services"
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
    for_each = length(var.config.master_authorized_cidr_blocks) > 0 ? [1] : []
    content {
      dynamic "cidr_blocks" {
        for_each = var.config.master_authorized_cidr_blocks
        content {
          cidr_block   = cidr_blocks.value.cidr_block
          display_name = cidr_blocks.value.display_name
        }
      }
    }
  }

  resource_labels = var.config.labels

  # GKE beta APIs are enabled by ops/gcp/enable_substrate_cert_beta_apis.sh.
  # The setting is one-way and provider 5.45 cannot safely reconcile it: after
  # discovery it proposes replacing the cluster to remove the API block.
  lifecycle {
    ignore_changes = [enable_k8s_beta_apis]
  }

}

resource "google_container_node_pool" "cpu" {
  provider = google-beta

  name       = "${var.config.name_prefix}-cpu"
  location   = var.config.zone
  cluster    = google_container_cluster.recsys.name
  node_count = var.config.cpu_min_nodes

  autoscaling {
    min_node_count = var.config.cpu_min_nodes
    max_node_count = var.config.cpu_max_nodes
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    # This project normally runs at its regional CPU and SSD quotas. Recreate
    # one node at a time so an update never needs an extra 8-vCPU/70-GB surge
    # node. Workloads can be briefly unavailable while the node is replaced.
    max_surge       = 0
    max_unavailable = 1
  }

  node_config {
    machine_type    = var.config.cpu_machine_type
    disk_size_gb    = var.config.cpu_disk_size_gb
    disk_type       = "pd-balanced"
    image_type      = "COS_CONTAINERD"
    spot            = var.config.cpu_spot
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = merge(var.config.labels, {
      "recsys.ai/pool"     = "cpu-services"
      "recsys.ai/workload" = "data-platform"
    })
    tags = ["${var.config.name_prefix}-cpu"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  depends_on = [google_project_iam_member.gke_node_roles]
}

resource "google_container_node_pool" "llm_cpu" {
  count    = var.config.deploy_llm_inference && var.config.llm_node_pool_mode == "dedicated" ? 1 : 0
  provider = google-beta

  name       = "${var.config.name_prefix}-llm-cpu"
  location   = var.config.zone
  cluster    = google_container_cluster.recsys.name
  node_count = var.config.llm_cpu_min_nodes

  autoscaling {
    min_node_count = var.config.llm_cpu_min_nodes
    max_node_count = var.config.llm_cpu_max_nodes
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    # Keep upgrades within the same fixed quota envelope as the initial node.
    max_surge       = 0
    max_unavailable = 1
  }

  node_config {
    machine_type    = var.config.llm_cpu_machine_type
    disk_size_gb    = var.config.llm_cpu_disk_size_gb
    disk_type       = var.config.llm_cpu_disk_type
    image_type      = "COS_CONTAINERD"
    spot            = var.config.llm_cpu_spot
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = merge(var.config.labels, {
      "recsys.ai/pool"     = "llm-cpu"
      "recsys.ai/workload" = "llm-inference"
    })
    tags = ["${var.config.name_prefix}-llm-cpu"]

    taint {
      key    = "recsys.ai/workload"
      value  = "llm-inference"
      effect = "NO_SCHEDULE"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  depends_on = [google_project_iam_member.gke_node_roles]
}

resource "google_container_node_pool" "langfuse" {
  count    = var.config.deploy_langfuse && var.config.langfuse_backend_mode == "managed" ? 1 : 0
  provider = google-beta

  name       = "${var.config.name_prefix}-langfuse"
  location   = var.config.zone
  cluster    = google_container_cluster.recsys.name
  node_count = var.config.langfuse_node_count

  autoscaling {
    min_node_count = var.config.langfuse_node_count
    max_node_count = var.config.langfuse_node_count
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
    machine_type = var.config.langfuse_node_machine_type
    disk_size_gb = 100
    # The project has only 25 GiB of regional pd-balanced quota remaining.
    # Use durable pd-standard boot disks so the fixed three-node HA pool fits
    # inside the existing quota envelope without touching production pools.
    disk_type       = "pd-standard"
    image_type      = "COS_CONTAINERD"
    spot            = false
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = merge(var.config.labels, {
      "recsys.ai/pool"     = "langfuse"
      "recsys.ai/workload" = "langfuse"
    })
    tags = ["${var.config.name_prefix}-langfuse"]

    taint {
      key    = "recsys.ai/workload"
      value  = "langfuse"
      effect = "NO_SCHEDULE"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  depends_on = [google_project_iam_member.gke_node_roles]
}

resource "google_container_node_pool" "ml_system" {
  provider = google-beta

  name       = "${var.config.name_prefix}-ml-system"
  location   = var.config.zone
  cluster    = google_container_cluster.recsys.name
  node_count = var.config.ml_min_nodes

  autoscaling {
    min_node_count = var.config.ml_min_nodes
    max_node_count = var.config.ml_max_nodes
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = var.config.capacity_profile == "compact-12vcpu" ? 0 : 1
    max_unavailable = var.config.capacity_profile == "compact-12vcpu" ? 1 : 0
  }

  node_config {
    machine_type    = var.config.ml_machine_type
    disk_size_gb    = var.config.ml_disk_size_gb
    disk_type       = "pd-balanced"
    image_type      = "COS_CONTAINERD"
    spot            = var.config.ml_spot
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = merge(var.config.labels, {
      "recsys.ai/pool"     = "ml-system"
      "recsys.ai/workload" = "ml-system"
    })
    tags = ["${var.config.name_prefix}-ml-system"]

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
  count    = var.config.enable_gpu_pool ? 1 : 0
  provider = google-beta

  name       = "${var.config.name_prefix}-gpu"
  location   = var.config.zone
  cluster    = google_container_cluster.recsys.name
  node_count = var.config.gpu_min_nodes

  autoscaling {
    min_node_count = var.config.gpu_min_nodes
    max_node_count = var.config.gpu_max_nodes
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = var.config.gpu_machine_type
    disk_size_gb    = var.config.gpu_disk_size_gb
    disk_type       = "pd-balanced"
    image_type      = "COS_CONTAINERD"
    spot            = var.config.gpu_spot
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = merge(var.config.labels, {
      "recsys.ai/pool" = "gpu-ml"
    })
    tags = ["${var.config.name_prefix}-gpu"]

    guest_accelerator {
      type  = var.config.gpu_accelerator_type
      count = var.config.gpu_accelerator_count

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
