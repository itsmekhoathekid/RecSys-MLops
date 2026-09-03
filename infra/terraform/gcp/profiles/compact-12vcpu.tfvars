# Apply after the environment's normal terraform.tfvars:
# terraform plan -var-file=terraform.tfvars -var-file=profiles/compact-12vcpu.tfvars
capacity_profile = "compact-12vcpu"

cpu_machine_type = "n2-standard-8"
cpu_min_nodes    = 1
cpu_max_nodes    = 1
cpu_disk_size_gb = 50
cpu_disk_type    = "pd-standard"

ml_machine_type = "e2-standard-4"
ml_min_nodes    = 1
ml_max_nodes    = 1
ml_disk_size_gb = 50
ml_disk_type    = "pd-standard"

llm_node_pool_mode = "cpu-services-shared"
enable_gpu_pool    = false
gpu_min_nodes      = 0
gpu_max_nodes      = 0

# The compact profile keeps these platform capabilities in the cluster. Data,
# analytics and demo releases are suspended by Helm, not destroyed by Terraform.
install_kubeflow_pipelines    = true
install_kserve                = true
scale_optional_kfp_components = true
deploy_ray_job                = false
deploy_serving                = true
deploy_model_serving          = true
deploy_llm_inference          = true
deploy_gateway                = true
deploy_service_mesh           = true
deploy_vault                  = true
deploy_agent_registry         = true
deploy_langfuse               = true
langfuse_backend_mode         = "in_cluster"
