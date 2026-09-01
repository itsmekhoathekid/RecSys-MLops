locals {
  module_config = {
    project_id                                   = var.project_id
    region                                       = var.region
    zone                                         = var.zone
    name_prefix                                  = var.name_prefix
    labels                                       = var.labels
    release_channel                              = var.release_channel
    deletion_protection                          = var.deletion_protection
    vpc_cidr                                     = var.vpc_cidr
    pods_cidr                                    = var.pods_cidr
    services_cidr                                = var.services_cidr
    master_authorized_cidr_blocks                = var.master_authorized_cidr_blocks
    cpu_machine_type                             = var.cpu_machine_type
    cpu_min_nodes                                = var.cpu_min_nodes
    cpu_max_nodes                                = var.cpu_max_nodes
    cpu_disk_size_gb                             = var.cpu_disk_size_gb
    cpu_spot                                     = var.cpu_spot
    llm_cpu_machine_type                         = var.llm_cpu_machine_type
    llm_cpu_min_nodes                            = var.llm_cpu_min_nodes
    llm_cpu_max_nodes                            = var.llm_cpu_max_nodes
    llm_cpu_disk_size_gb                         = var.llm_cpu_disk_size_gb
    llm_cpu_disk_type                            = var.llm_cpu_disk_type
    llm_cpu_spot                                 = var.llm_cpu_spot
    llm_node_pool_mode                           = var.llm_node_pool_mode
    llm_optimization_profile                     = var.llm_optimization_profile
    ml_machine_type                              = var.ml_machine_type
    ml_min_nodes                                 = var.ml_min_nodes
    ml_max_nodes                                 = var.ml_max_nodes
    ml_disk_size_gb                              = var.ml_disk_size_gb
    ml_spot                                      = var.ml_spot
    gpu_machine_type                             = var.gpu_machine_type
    enable_gpu_pool                              = var.enable_gpu_pool
    gpu_accelerator_type                         = var.gpu_accelerator_type
    gpu_accelerator_count                        = var.gpu_accelerator_count
    gpu_min_nodes                                = var.gpu_min_nodes
    gpu_max_nodes                                = var.gpu_max_nodes
    gpu_disk_size_gb                             = var.gpu_disk_size_gb
    gpu_spot                                     = var.gpu_spot
    artifact_registry_repository                 = var.artifact_registry_repository
    image_tag                                    = var.image_tag
    image_overrides                              = var.image_overrides
    kubeflow_pipelines_version                   = var.kubeflow_pipelines_version
    kserve_version                               = var.kserve_version
    install_kubeflow_pipelines                   = var.install_kubeflow_pipelines
    install_kserve                               = var.install_kserve
    scale_optional_kfp_components                = var.scale_optional_kfp_components
    deploy_ray_job                               = var.deploy_ray_job
    deploy_serving                               = var.deploy_serving
    deploy_model_serving                         = var.deploy_model_serving
    deploy_llm_inference                         = var.deploy_llm_inference
    agent_gateway_auth_enabled                   = var.agent_gateway_auth_enabled
    agentgateway_version                         = var.agentgateway_version
    llm_d_router_chart_version                   = var.llm_d_router_chart_version
    gateway_api_version                          = var.gateway_api_version
    gateway_api_inference_extension_version      = var.gateway_api_inference_extension_version
    deploy_gateway                               = var.deploy_gateway
    deploy_datahub                               = var.deploy_datahub
    deploy_service_mesh                          = var.deploy_service_mesh
    deploy_vault                                 = var.deploy_vault
    vault_chart_version                          = var.vault_chart_version
    vault_replicas                               = var.vault_replicas
    vault_storage_size                           = var.vault_storage_size
    vault_kms_location                           = var.vault_kms_location
    vault_legacy_source_secrets_enabled          = var.vault_legacy_source_secrets_enabled
    gateway_domain                               = var.gateway_domain
    gateway_tls_enabled                          = var.gateway_tls_enabled
    gateway_tls_cluster_issuer                   = var.gateway_tls_cluster_issuer
    gateway_tls_issuer_create                    = var.gateway_tls_issuer_create
    gateway_tls_issuer_email                     = var.gateway_tls_issuer_email
    gateway_tls_issuer_server                    = var.gateway_tls_issuer_server
    gateway_htpasswd                             = var.gateway_htpasswd
    datahub_mysql_root_password                  = var.datahub_mysql_root_password
    datahub_mysql_replication_password           = var.datahub_mysql_replication_password
    datahub_mysql_password                       = var.datahub_mysql_password
    datahub_mysql_cdc_password                   = var.datahub_mysql_cdc_password
    datahub_encryption_key_secret                = var.datahub_encryption_key_secret
    kagent_version                               = var.kagent_version
    agent_substrate_version                      = var.agent_substrate_version
    deploy_agent_registry                        = var.deploy_agent_registry
    agentregistry_version                        = var.agentregistry_version
    deploy_langfuse                              = var.deploy_langfuse
    langfuse_backend_mode                        = var.langfuse_backend_mode
    langfuse_managed_backend_deletion_protection = var.langfuse_managed_backend_deletion_protection
    langfuse_domain                              = var.langfuse_domain
    langfuse_chart_version                       = var.langfuse_chart_version
    langfuse_app_version                         = var.langfuse_app_version
    langfuse_node_machine_type                   = var.langfuse_node_machine_type
    langfuse_node_count                          = var.langfuse_node_count
    langfuse_retention_days                      = var.langfuse_retention_days
  }
}

module "project_services" {
  source = "./modules/project-services"

  config = local.module_config

  providers = {
    google = google
  }
}

module "network" {
  source = "./modules/network"

  config          = local.module_config
  api_service_ids = module.project_services.required_service_ids

  providers = {
    google = google
  }

}

module "artifact_storage" {
  source = "./modules/artifact-storage"

  config          = local.module_config
  api_service_ids = module.project_services.required_service_ids

  providers = {
    google = google
  }

}

module "gke" {
  source = "./modules/gke"

  config          = local.module_config
  api_service_ids = module.project_services.required_service_ids
  project_number  = data.google_project.current.number
  network_id      = module.network.network_id
  subnetwork_id   = module.network.subnetwork_id

  providers = {
    google      = google
    google-beta = google-beta
  }

}

module "kubernetes_platform" {
  source = "./modules/kubernetes-platform"

  config          = local.module_config
  helm_dir        = "${path.module}/../../helm"
  repo_root       = "${path.module}/../../.."
  api_service_ids = module.project_services.required_service_ids
  cluster = {
    id                            = module.gke.cluster_id
    name                          = module.gke.cluster_name
    endpoint                      = module.gke.cluster_endpoint
    cpu_node_pool_name            = module.gke.cpu_node_pool_name
    network_id                    = module.network.network_id
    private_service_connection_id = module.network.private_service_connection_id
  }

  providers = {
    google     = google
    helm       = helm
    kubernetes = kubernetes
  }

  depends_on = [
    null_resource.cluster_credentials,
  ]

}
