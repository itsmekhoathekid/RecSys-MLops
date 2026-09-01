# The exclusion was installed with gcloud during the cost-control rollout.
# Import blocks must remain in the root module even when the managed resource
# lives in a child module.
import {
  id = "projects/${var.project_id}/exclusions/drop-k8s-low-severity"
  to = module.project_services.google_logging_project_exclusion.drop_k8s_low_severity
}
