output "required_service_ids" {
  description = "Enabled Google API resource IDs used to order dependent modules."
  value       = values(google_project_service.required)[*].id
}
