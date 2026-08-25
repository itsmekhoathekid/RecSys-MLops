variable "project_id" {
  description = "GCP project id that owns the RecSys MLOps deployment."
  type        = string
}

variable "region" {
  description = "GCP region for regional resources. asia-southeast1 keeps latency reasonable for Vietnam/Singapore traffic."
  type        = string
  default     = "asia-southeast1"
}

variable "zone" {
  description = "GKE zone. Pick a zone with quota for the selected GPU accelerator."
  type        = string
  default     = "asia-southeast1-b"
}

variable "name_prefix" {
  description = "Prefix for GCP resources."
  type        = string
  default     = "recsys-mlops"
}

variable "labels" {
  description = "Labels applied to GCP resources."
  type        = map(string)
  default = {
    app          = "recsys-mlops"
    "managed-by" = "terraform"
    cost-profile = "moderate"
  }
}

variable "release_channel" {
  description = "GKE release channel."
  type        = string
  default     = "REGULAR"
}

variable "deletion_protection" {
  description = "Protect the GKE cluster from terraform destroy."
  type        = bool
  default     = false
}

variable "vpc_cidr" {
  description = "Primary subnet CIDR for GKE nodes."
  type        = string
  default     = "10.40.0.0/20"
}

variable "pods_cidr" {
  description = "Secondary CIDR for GKE Pods."
  type        = string
  default     = "10.44.0.0/14"
}

variable "services_cidr" {
  description = "Secondary CIDR for GKE Services."
  type        = string
  default     = "10.48.0.0/20"
}

variable "master_authorized_cidr_blocks" {
  description = "Optional CIDR blocks allowed to reach the public GKE control plane endpoint."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []
}

variable "cpu_machine_type" {
  description = "Machine type for always-on data/API/system workloads."
  type        = string
  default     = "e2-standard-4"
}

variable "cpu_min_nodes" {
  description = "Minimum CPU nodes. Keep at least 2 for Kafka/Postgres/Airflow and API availability."
  type        = number
  default     = 2
}

variable "cpu_max_nodes" {
  description = "Maximum CPU nodes."
  type        = number
  default     = 5
}

variable "cpu_disk_size_gb" {
  description = "Boot disk size for CPU nodes."
  type        = number
  default     = 100
}

variable "cpu_spot" {
  description = "Use Spot VMs for CPU node pool. Leave false for stateful services."
  type        = bool
  default     = false
}

variable "llm_cpu_machine_type" {
  description = "Machine type for the dedicated CPU-only llm-d model-serving pool."
  type        = string
  default     = "n2-standard-4"
}

variable "llm_cpu_min_nodes" {
  description = "Minimum nodes in the dedicated llm-d CPU pool."
  type        = number
  default     = 1
}

variable "llm_cpu_max_nodes" {
  description = "Maximum nodes in the dedicated llm-d CPU pool."
  type        = number
  default     = 2
}

variable "llm_cpu_disk_size_gb" {
  description = "Boot disk size for dedicated llm-d CPU nodes."
  type        = number
  default     = 30
}

variable "llm_cpu_disk_type" {
  description = "Boot disk type for dedicated llm-d CPU nodes. pd-standard avoids consuming the exhausted SSD quota."
  type        = string
  default     = "pd-standard"
}

variable "llm_cpu_spot" {
  description = "Use Spot VMs for llm-d CPU inference. Keep false for reproducible benchmarks."
  type        = bool
  default     = false
}

variable "llm_node_pool_mode" {
  description = "LLM placement mode: dedicated creates the llm-cpu pool; cpu-services-shared reuses the existing recsys-mlops-cpu node pool."
  type        = string
  default     = "dedicated"

  validation {
    condition     = contains(["dedicated", "cpu-services-shared"], var.llm_node_pool_mode)
    error_message = "llm_node_pool_mode must be dedicated or cpu-services-shared."
  }
}

variable "llm_optimization_profile" {
  description = "LLM treatment profile: baseline uses uniform random routing; optimized adds llm-d inflight token load-aware routing. Both profiles use the same llama.cpp GGUF servers."
  type        = string
  default     = "baseline"

  validation {
    condition     = contains(["baseline", "optimized"], var.llm_optimization_profile)
    error_message = "llm_optimization_profile must be baseline or optimized."
  }
}

variable "ml_machine_type" {
  description = "Machine type for the dedicated ML system node pool used by MLflow, API serving, and Triton/KServe."
  type        = string
  default     = "e2-standard-4"
}

variable "ml_min_nodes" {
  description = "Minimum ML system nodes."
  type        = number
  default     = 1
}

variable "ml_max_nodes" {
  description = "Maximum ML system nodes."
  type        = number
  default     = 2
}

variable "ml_disk_size_gb" {
  description = "Boot disk size for ML system nodes."
  type        = number
  default     = 30
}

variable "ml_spot" {
  description = "Use Spot VMs for the ML system node pool."
  type        = bool
  default     = false
}

variable "gpu_machine_type" {
  description = "GPU node machine type. n1-standard-8 plus one T4 is a moderate cost/latency default."
  type        = string
  default     = "n1-standard-8"
}

variable "enable_gpu_pool" {
  description = "Create the T4 node pool and use GPU Ray/KServe values. Disable for the supported CPU fallback profile."
  type        = bool
  default     = true
}

variable "gpu_accelerator_type" {
  description = "GKE accelerator type for Ray training and Triton inference."
  type        = string
  default     = "nvidia-tesla-t4"
}

variable "gpu_accelerator_count" {
  description = "GPU count per GPU node."
  type        = number
  default     = 1
}

variable "gpu_min_nodes" {
  description = "Minimum GPU nodes. One warm node avoids Triton cold start; set 0 for dev cost saving."
  type        = number
  default     = 1
}

variable "gpu_max_nodes" {
  description = "Maximum GPU nodes. Keep low to cap runaway training/inference cost."
  type        = number
  default     = 2
}

variable "gpu_disk_size_gb" {
  description = "Boot disk size for GPU nodes."
  type        = number
  default     = 100
}

variable "gpu_spot" {
  description = "Use Spot VMs for GPU node pool. Cheaper, but not recommended for always-on Triton."
  type        = bool
  default     = false
}

variable "artifact_registry_repository" {
  description = "Artifact Registry Docker repository name."
  type        = string
  default     = "recsys"
}

variable "image_tag" {
  description = "Default image tag used for RecSys images in Artifact Registry."
  type        = string
  default     = "gcp"
}

variable "image_overrides" {
  description = "Optional full image overrides. Keys: data_ingestion, feature_store, drift_retrain, spark, flink, kafka_connect, airflow, mlflow, api, training_repository."
  type        = map(string)
  default     = {}
}

variable "kubeflow_pipelines_version" {
  description = "Kubeflow Pipelines manifest version."
  type        = string
  default     = "2.16.1"
}

variable "kserve_version" {
  description = "KServe manifest version."
  type        = string
  default     = "v0.15.2"
}

variable "install_kubeflow_pipelines" {
  description = "Install Kubeflow Pipelines with kubectl/kustomize from the upstream manifests."
  type        = bool
  default     = true
}

variable "install_kserve" {
  description = "Install KServe CRDs, controller, and cluster serving runtimes."
  type        = bool
  default     = true
}

variable "scale_optional_kfp_components" {
  description = "Scale nonessential KFP components down to reduce steady-state cost."
  type        = bool
  default     = true
}

variable "deploy_ray_job" {
  description = "Deploy a standalone bootstrap RayJob. Keep false for production drift-triggered KFP retraining."
  type        = bool
  default     = false
}

variable "deploy_serving" {
  description = "Deploy the online-feature and recommendation inference APIs."
  type        = bool
  default     = true
}

variable "deploy_model_serving" {
  description = "Deploy the KServe/Triton model release. Keep false until training has promoted a model into the serving object-store prefix."
  type        = bool
  default     = true
}

variable "deploy_llm_inference" {
  description = "Deploy the llm-d, agentgateway, and CPU llama.cpp GGUF model-serving stack."
  type        = bool
  default     = false
}

variable "agent_gateway_auth_enabled" {
  description = "Require a Vault-backed API key on every request to the llm-d agentgateway."
  type        = bool
  default     = true
}

variable "agentgateway_version" {
  description = "Pinned agentgateway CRD and controller chart version."
  type        = string
  default     = "v1.1.0"
}

variable "llm_d_router_chart_version" {
  description = "Pinned llm-d Router Gateway chart version."
  type        = string
  default     = "v0.9.0"
}

variable "gateway_api_version" {
  description = "Pinned Kubernetes Gateway API CRD version required by llm-d."
  type        = string
  default     = "v1.5.1"
}

variable "gateway_api_inference_extension_version" {
  description = "Pinned Gateway API Inference Extension CRD version required by llm-d."
  type        = string
  default     = "v1.5.0"
}

variable "deploy_gateway" {
  description = "Deploy ingress-nginx and the public RecSys gateway. Requires DNS/TLS planning."
  type        = bool
  default     = false
}

variable "deploy_datahub" {
  description = "Deploy DataHub metadata governance services and prerequisites."
  type        = bool
  default     = false
}

variable "deploy_service_mesh" {
  description = "Deploy Istio service mesh control plane and RecSys mTLS/authorization policies."
  type        = bool
  default     = true
}

variable "deploy_vault" {
  description = "Deploy HashiCorp Vault HA with Raft storage and GCP Cloud KMS auto-unseal."
  type        = bool
  default     = false
}

variable "vault_chart_version" {
  description = "Pinned official HashiCorp Vault Helm chart version."
  type        = string
  default     = "0.34.0"
}

variable "vault_replicas" {
  description = "Number of Vault HA Raft replicas. Use an odd number for quorum."
  type        = number
  default     = 3

  validation {
    condition     = var.vault_replicas >= 3 && var.vault_replicas % 2 == 1
    error_message = "vault_replicas must be an odd number greater than or equal to 3."
  }
}

variable "vault_storage_size" {
  description = "Persistent disk size for each Vault Raft replica."
  type        = string
  default     = "10Gi"
}

variable "vault_kms_location" {
  description = "GCP Cloud KMS location used by Vault auto-unseal."
  type        = string
  default     = "global"
}

variable "vault_legacy_source_secrets_enabled" {
  description = "Keep the pre-Vault source Kubernetes Secrets during migration. Disable only after Vault-backed ExternalSecrets are Ready."
  type        = bool
  default     = true
}

variable "gateway_domain" {
  description = "Domain used by the optional gateway chart."
  type        = string
  default     = "example.invalid"
}

variable "gateway_tls_enabled" {
  description = "Enable HTTPS and cert-manager certificates for public gateway routes."
  type        = bool
  default     = false
}

variable "gateway_tls_cluster_issuer" {
  description = "Existing cert-manager ClusterIssuer used by public gateway routes."
  type        = string
  default     = "letsencrypt-prod"
}

variable "gateway_tls_issuer_create" {
  description = "Create the cert-manager ClusterIssuer as part of the gateway release."
  type        = bool
  default     = false
}

variable "gateway_tls_issuer_email" {
  description = "ACME account email used when the gateway creates its ClusterIssuer."
  type        = string
  default     = ""
}

variable "gateway_tls_issuer_server" {
  description = "ACME directory URL used when the gateway creates its ClusterIssuer."
  type        = string
  default     = "https://acme-v02.api.letsencrypt.org/directory"
}

variable "gateway_htpasswd" {
  description = "Rotated htpasswd line for gateway basic auth, for example user:hash. Set via TF_VAR_gateway_htpasswd from the ignored .env file."
  type        = string
  default     = null
  sensitive   = true
}

variable "datahub_mysql_root_password" {
  description = "Optional rotated DataHub MySQL root password. If null, Terraform generates one."
  type        = string
  default     = null
  sensitive   = true
}

variable "datahub_mysql_replication_password" {
  description = "Optional rotated DataHub MySQL replication password. If null, Terraform generates one."
  type        = string
  default     = null
  sensitive   = true
}

variable "datahub_mysql_password" {
  description = "Optional rotated DataHub MySQL application password. If null, Terraform generates one."
  type        = string
  default     = null
  sensitive   = true
}

variable "datahub_mysql_cdc_password" {
  description = "Optional rotated DataHub MySQL CDC password. If null, Terraform generates one."
  type        = string
  default     = null
  sensitive   = true
}

variable "datahub_encryption_key_secret" {
  description = "Optional rotated DataHub encryption key secret. If null, Terraform generates one."
  type        = string
  default     = null
  sensitive   = true
}
