resource "null_resource" "cluster_credentials" {
  triggers = {
    cluster_id = module.gke.cluster_id
    endpoint   = module.gke.cluster_endpoint
  }

  provisioner "local-exec" {
    command = "gcloud container clusters get-credentials ${module.gke.cluster_name} --zone ${var.zone} --project ${var.project_id}"
  }

  depends_on = [module.gke]
}
