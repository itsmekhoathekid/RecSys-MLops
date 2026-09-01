moved {
  from = google_artifact_registry_repository.docker
  to   = module.artifact_storage.google_artifact_registry_repository.docker
}

moved {
  from = google_storage_bucket.lake_backup
  to   = module.artifact_storage.google_storage_bucket.lake_backup
}

moved {
  from = google_storage_bucket.model_backup
  to   = module.artifact_storage.google_storage_bucket.model_backup
}

moved {
  from = google_container_cluster.recsys
  to   = module.gke.google_container_cluster.recsys
}

moved {
  from = google_container_node_pool.cpu
  to   = module.gke.google_container_node_pool.cpu
}

moved {
  from = google_container_node_pool.gpu
  to   = module.gke.google_container_node_pool.gpu
}

moved {
  from = google_container_node_pool.llm_cpu
  to   = module.gke.google_container_node_pool.llm_cpu
}

moved {
  from = google_container_node_pool.ml_system
  to   = module.gke.google_container_node_pool.ml_system
}

moved {
  from = google_project_iam_member.atelet_workload_identity_artifact_registry_reader
  to   = module.gke.google_project_iam_member.atelet_workload_identity_artifact_registry_reader
}

moved {
  from = google_project_iam_member.gke_node_roles
  to   = module.gke.google_project_iam_member.gke_node_roles
}

moved {
  from = google_project_iam_member.jenkins_workload_identity_artifact_registry_writer
  to   = module.gke.google_project_iam_member.jenkins_workload_identity_artifact_registry_writer
}

moved {
  from = google_service_account.gke_nodes
  to   = module.gke.google_service_account.gke_nodes
}

moved {
  from = google_kms_crypto_key_iam_member.vault_unseal
  to   = module.kubernetes_platform.google_kms_crypto_key_iam_member.vault_unseal
}

moved {
  from = google_kms_crypto_key_iam_member.vault_unseal_metadata
  to   = module.kubernetes_platform.google_kms_crypto_key_iam_member.vault_unseal_metadata
}

moved {
  from = google_kms_crypto_key.vault_unseal
  to   = module.kubernetes_platform.google_kms_crypto_key.vault_unseal
}

moved {
  from = google_kms_key_ring.vault
  to   = module.kubernetes_platform.google_kms_key_ring.vault
}

moved {
  from = google_service_account_iam_member.vault_workload_identity
  to   = module.kubernetes_platform.google_service_account_iam_member.vault_workload_identity
}

moved {
  from = google_service_account.vault
  to   = module.kubernetes_platform.google_service_account.vault
}

moved {
  from = helm_release.agentgateway
  to   = module.kubernetes_platform.helm_release.agentgateway
}

moved {
  from = helm_release.agentgateway_crds
  to   = module.kubernetes_platform.helm_release.agentgateway_crds
}

moved {
  from = helm_release.agentregistry
  to   = module.kubernetes_platform.helm_release.agentregistry
}

moved {
  from = helm_release.agentregistry_postgres
  to   = module.kubernetes_platform.helm_release.agentregistry_postgres
}

moved {
  from = helm_release.cert_manager
  to   = module.kubernetes_platform.helm_release.cert_manager
}

moved {
  from = helm_release.datahub
  to   = module.kubernetes_platform.helm_release.datahub
}

moved {
  from = helm_release.datahub_prerequisites
  to   = module.kubernetes_platform.helm_release.datahub_prerequisites
}

moved {
  from = helm_release.external_secrets
  to   = module.kubernetes_platform.helm_release.external_secrets
}

moved {
  from = helm_release.ingress_nginx
  to   = module.kubernetes_platform.helm_release.ingress_nginx
}

moved {
  from = helm_release.istio_base
  to   = module.kubernetes_platform.helm_release.istio_base
}

moved {
  from = helm_release.istiod
  to   = module.kubernetes_platform.helm_release.istiod
}

moved {
  from = helm_release.kagent
  to   = module.kubernetes_platform.helm_release.kagent
}

moved {
  from = helm_release.kagent_crds
  to   = module.kubernetes_platform.helm_release.kagent_crds
}

moved {
  from = helm_release.keda
  to   = module.kubernetes_platform.helm_release.keda
}

moved {
  from = helm_release.keda_http
  to   = module.kubernetes_platform.helm_release.keda_http
}

moved {
  from = helm_release.kuberay_operator
  to   = module.kubernetes_platform.helm_release.kuberay_operator
}

moved {
  from = helm_release.llm_d_router
  to   = module.kubernetes_platform.helm_release.llm_d_router
}

moved {
  from = helm_release.prometheus_operator
  to   = module.kubernetes_platform.helm_release.prometheus_operator
}

moved {
  from = helm_release.recsys_airflow
  to   = module.kubernetes_platform.helm_release.recsys_airflow
}

moved {
  from = helm_release.recsys_data_config
  to   = module.kubernetes_platform.helm_release.recsys_data_config
}

moved {
  from = helm_release.recsys_data_lakehouse
  to   = module.kubernetes_platform.helm_release.recsys_data_lakehouse
}

moved {
  from = helm_release.recsys_event_stream
  to   = module.kubernetes_platform.helm_release.recsys_event_stream
}

moved {
  from = helm_release.recsys_feature_store
  to   = module.kubernetes_platform.helm_release.recsys_feature_store
}

moved {
  from = helm_release.recsys_gateway
  to   = module.kubernetes_platform.helm_release.recsys_gateway
}

moved {
  from = helm_release.recsys_inference_api
  to   = module.kubernetes_platform.helm_release.recsys_inference_api
}

moved {
  from = helm_release.recsys_kafka_connect
  to   = module.kubernetes_platform.helm_release.recsys_kafka_connect
}

moved {
  from = helm_release.recsys_llm_serving
  to   = module.kubernetes_platform.helm_release.recsys_llm_serving
}

moved {
  from = helm_release.recsys_mlflow
  to   = module.kubernetes_platform.helm_release.recsys_mlflow
}

moved {
  from = helm_release.recsys_observability
  to   = module.kubernetes_platform.helm_release.recsys_observability
}

moved {
  from = helm_release.recsys_online_feature_api
  to   = module.kubernetes_platform.helm_release.recsys_online_feature_api
}

moved {
  from = helm_release.recsys_ray_gpu
  to   = module.kubernetes_platform.helm_release.recsys_ray_gpu
}

moved {
  from = helm_release.recsys_runtime
  to   = module.kubernetes_platform.helm_release.recsys_runtime
}

moved {
  from = helm_release.recsys_security
  to   = module.kubernetes_platform.helm_release.recsys_security
}

moved {
  from = helm_release.recsys_serving
  to   = module.kubernetes_platform.helm_release.recsys_serving
}

moved {
  from = helm_release.recsys_source_store
  to   = module.kubernetes_platform.helm_release.recsys_source_store
}

moved {
  from = helm_release.recsys_streaming
  to   = module.kubernetes_platform.helm_release.recsys_streaming
}

moved {
  from = helm_release.substrate
  to   = module.kubernetes_platform.helm_release.substrate
}

moved {
  from = helm_release.substrate_crds
  to   = module.kubernetes_platform.helm_release.substrate_crds
}

moved {
  from = helm_release.substrate_mtls_bootstrap
  to   = module.kubernetes_platform.helm_release.substrate_mtls_bootstrap
}

moved {
  from = helm_release.vault
  to   = module.kubernetes_platform.helm_release.vault
}

moved {
  from = kubernetes_cluster_role_binding_v1.keda_workerpool_scaler
  to   = module.kubernetes_platform.kubernetes_cluster_role_binding_v1.keda_workerpool_scaler
}

moved {
  from = kubernetes_cluster_role_binding_v1.vault_token_reviewer
  to   = module.kubernetes_platform.kubernetes_cluster_role_binding_v1.vault_token_reviewer
}

moved {
  from = kubernetes_cluster_role_v1.keda_workerpool_scaler
  to   = module.kubernetes_platform.kubernetes_cluster_role_v1.keda_workerpool_scaler
}

moved {
  from = kubernetes_labels.ingress_nginx_mesh
  to   = module.kubernetes_platform.kubernetes_labels.ingress_nginx_mesh
}

moved {
  from = kubernetes_manifest.recsys_coordinator_sandbox_pool
  to   = module.kubernetes_platform.kubernetes_manifest.recsys_coordinator_sandbox_pool
}

moved {
  from = kubernetes_manifest.recsys_recommendation_sandbox_pool
  to   = module.kubernetes_platform.kubernetes_manifest.recsys_recommendation_sandbox_pool
}

moved {
  from = kubernetes_namespace.agentregistry
  to   = module.kubernetes_platform.kubernetes_namespace.agentregistry
}

moved {
  from = kubernetes_namespace.api_serving
  to   = module.kubernetes_platform.kubernetes_namespace.api_serving
}

moved {
  from = kubernetes_namespace.ate_system
  to   = module.kubernetes_platform.kubernetes_namespace.ate_system
}

moved {
  from = kubernetes_namespace.datahub
  to   = module.kubernetes_platform.kubernetes_namespace.datahub
}

moved {
  from = kubernetes_namespace.experiment_tracking
  to   = module.kubernetes_platform.kubernetes_namespace.experiment_tracking
}

moved {
  from = kubernetes_namespace.kagent
  to   = module.kubernetes_platform.kubernetes_namespace.kagent
}

moved {
  from = kubernetes_namespace.kserve_triton_inference
  to   = module.kubernetes_platform.kubernetes_namespace.kserve_triton_inference
}

moved {
  from = kubernetes_namespace.llm_inference
  to   = module.kubernetes_platform.kubernetes_namespace.llm_inference
}

moved {
  from = kubernetes_namespace.observability
  to   = module.kubernetes_platform.kubernetes_namespace.observability
}

moved {
  from = kubernetes_namespace.podcertificate_controller_system
  to   = module.kubernetes_platform.kubernetes_namespace.podcertificate_controller_system
}

moved {
  from = kubernetes_namespace.recsys_dataflow
  to   = module.kubernetes_platform.kubernetes_namespace.recsys_dataflow
}

moved {
  from = kubernetes_persistent_volume_claim_v1.substrate_rustfs
  to   = module.kubernetes_platform.kubernetes_persistent_volume_claim_v1.substrate_rustfs
}

moved {
  from = kubernetes_persistent_volume_claim_v1.substrate_valkey
  to   = module.kubernetes_platform.kubernetes_persistent_volume_claim_v1.substrate_valkey
}

moved {
  from = kubernetes_secret_v1.centralized_recsys
  to   = module.kubernetes_platform.kubernetes_secret_v1.centralized_recsys
}

moved {
  from = kubernetes_secret_v1.kagent_agent_gateway
  to   = module.kubernetes_platform.kubernetes_secret_v1.kagent_agent_gateway
}

moved {
  from = kubernetes_secret.datahub_encryption
  to   = module.kubernetes_platform.kubernetes_secret.datahub_encryption
}

moved {
  from = kubernetes_secret.datahub_mysql
  to   = module.kubernetes_platform.kubernetes_secret.datahub_mysql
}

moved {
  from = kubernetes_service_v1.datahub_kafka_alias
  to   = module.kubernetes_platform.kubernetes_service_v1.datahub_kafka_alias
}

moved {
  from = null_resource.kserve
  to   = module.kubernetes_platform.null_resource.kserve
}

moved {
  from = null_resource.kubeflow_pipelines
  to   = module.kubernetes_platform.null_resource.kubeflow_pipelines
}

moved {
  from = null_resource.llm_gateway_api_crds
  to   = module.kubernetes_platform.null_resource.llm_gateway_api_crds
}

moved {
  from = null_resource.recsys_external_secrets_ready
  to   = module.kubernetes_platform.null_resource.recsys_external_secrets_ready
}

moved {
  from = random_password.agentregistry_postgres
  to   = module.kubernetes_platform.random_password.agentregistry_postgres
}

moved {
  from = random_password.airflow_postgres
  to   = module.kubernetes_platform.random_password.airflow_postgres
}

moved {
  from = random_password.datahub_encryption_key
  to   = module.kubernetes_platform.random_password.datahub_encryption_key
}

moved {
  from = random_password.datahub_mysql
  to   = module.kubernetes_platform.random_password.datahub_mysql
}

moved {
  from = random_password.datahub_mysql_cdc
  to   = module.kubernetes_platform.random_password.datahub_mysql_cdc
}

moved {
  from = random_password.datahub_mysql_replication
  to   = module.kubernetes_platform.random_password.datahub_mysql_replication
}

moved {
  from = random_password.datahub_mysql_root
  to   = module.kubernetes_platform.random_password.datahub_mysql_root
}

moved {
  from = random_password.feast_postgres
  to   = module.kubernetes_platform.random_password.feast_postgres
}

moved {
  from = random_password.gateway_basic_auth
  to   = module.kubernetes_platform.random_password.gateway_basic_auth
}

moved {
  from = random_password.milvus_root
  to   = module.kubernetes_platform.random_password.milvus_root
}

moved {
  from = random_password.minio_root
  to   = module.kubernetes_platform.random_password.minio_root
}

moved {
  from = random_password.mlflow_postgres
  to   = module.kubernetes_platform.random_password.mlflow_postgres
}

moved {
  from = random_password.source_postgres
  to   = module.kubernetes_platform.random_password.source_postgres
}

moved {
  from = google_compute_network.recsys
  to   = module.network.google_compute_network.recsys
}

moved {
  from = google_compute_subnetwork.gke
  to   = module.network.google_compute_subnetwork.gke
}

moved {
  from = google_logging_project_exclusion.drop_k8s_low_severity
  to   = module.project_services.google_logging_project_exclusion.drop_k8s_low_severity
}

moved {
  from = google_project_service.required
  to   = module.project_services.google_project_service.required
}
