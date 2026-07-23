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
  default     = 1
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
  description = "Optional full image overrides. Keys: dataflow_cli, spark, flink, kafka_connect, airflow, mlflow, api, training_repository."
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
  description = "Deploy the Ray GPU training RayJob."
  type        = bool
  default     = true
}

variable "deploy_serving" {
  description = "Deploy the KServe/Triton GPU serving chart. Disable when GPU billing/quota is unavailable."
  type        = bool
  default     = true
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

variable "gateway_domain" {
  description = "Domain used by the optional gateway chart."
  type        = string
  default     = "recsys.local"
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
