resource "google_project_service" "required" {
  for_each = toset(concat([
    "artifactregistry.googleapis.com",
    "cloudkms.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    ], var.config.deploy_langfuse && var.config.langfuse_backend_mode == "managed" ? [
    "gkebackup.googleapis.com",
    "networkconnectivity.googleapis.com",
    "redis.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
  ] : []))

  project            = var.config.project_id
  service            = each.key
  disable_on_destroy = false
}
