variable "config" {
  description = "Validated root deployment configuration."
  type        = any
}

variable "helm_dir" {
  description = "Absolute path to the repository Helm chart directory."
  type        = string
}

variable "repo_root" {
  description = "Absolute path to the repository root for configuration and operational scripts."
  type        = string
}

variable "api_service_ids" {
  description = "Enabled Google API resource IDs used to order managed platform dependencies."
  type        = list(string)
}

variable "cluster" {
  description = "GKE identifiers required by cluster bootstrap resources."
  type = object({
    id                            = string
    name                          = string
    endpoint                      = string
    cpu_node_pool_name            = string
    network_id                    = string
    private_service_connection_id = optional(string)
  })
}
