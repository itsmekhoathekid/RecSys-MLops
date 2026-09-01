variable "config" {
  description = "Validated root deployment configuration."
  type        = any
}

variable "api_service_ids" {
  description = "Enabled Google API IDs that must exist before GKE resources are created."
  type        = list(string)
}

variable "project_number" {
  description = "Numeric GCP project identifier used in Workload Identity principals."
  type        = string
}

variable "network_id" {
  description = "VPC network ID used by the GKE cluster."
  type        = string
}

variable "subnetwork_id" {
  description = "Subnetwork ID used by the GKE cluster."
  type        = string
}
