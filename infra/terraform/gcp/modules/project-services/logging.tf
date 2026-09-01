resource "google_logging_project_exclusion" "drop_k8s_low_severity" {
  project = var.config.project_id

  name        = "drop-k8s-low-severity"
  description = "Keep Kubernetes warnings and errors in Cloud Logging; INFO and DEBUG remain available through the in-cluster Promtail/Loki pipeline."
  filter      = <<-EOT
    resource.type = "k8s_container"
    severity < WARNING
  EOT
}
