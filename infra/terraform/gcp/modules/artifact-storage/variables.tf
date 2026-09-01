variable "config" {
  description = "Validated root deployment configuration."
  type        = any
}

variable "api_service_ids" {
  description = "Enabled Google API IDs that must exist before artifact storage is created."
  type        = list(string)
}
