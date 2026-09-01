variable "config" {
  description = "Validated root deployment configuration."
  type        = any
}

variable "api_service_ids" {
  description = "Enabled Google API IDs that must exist before networking is created."
  type        = list(string)
}
