locals {
  external_secret_source_namespace = "external-secrets"
  gateway_htpasswd                 = coalesce(var.config.gateway_htpasswd, "recsys:${bcrypt(random_password.gateway_basic_auth.result)}")

  external_secret_payloads = {
    data-platform = {
      DATA_PLATFORM_MINIO_ROOT_USER     = "minio"
      DATA_PLATFORM_MINIO_ROOT_PASSWORD = random_password.minio_root.result
      MINIO_ROOT_USER                   = "minio"
      MINIO_ROOT_PASSWORD               = random_password.minio_root.result
      AWS_ACCESS_KEY_ID                 = "minio"
      AWS_SECRET_ACCESS_KEY             = random_password.minio_root.result
      POSTGRES_USER                     = "recsys"
      POSTGRES_PASSWORD                 = random_password.source_postgres.result
      AIRFLOW_POSTGRES_USER             = "airflow"
      AIRFLOW_POSTGRES_PASSWORD         = random_password.airflow_postgres.result
      FEAST_POSTGRES_USER               = "feast"
      FEAST_POSTGRES_PASSWORD           = random_password.feast_postgres.result
      MILVUS_USERNAME                   = "root"
      MILVUS_PASSWORD                   = random_password.milvus_root.result
    }
    mlflow = {
      MINIO_ROOT_USER     = "minio"
      MINIO_ROOT_PASSWORD = random_password.minio_root.result
      POSTGRES_DB         = "mlflow"
      POSTGRES_USER       = "mlflow"
      POSTGRES_PASSWORD   = random_password.mlflow_postgres.result
    }
    runtime = {
      MINIO_ENDPOINT              = "http://data-platform-minio.recsys-dataflow.svc.cluster.local:9000"
      MINIO_ROOT_USER             = "minio"
      MINIO_ROOT_PASSWORD         = random_password.minio_root.result
      AWS_ACCESS_KEY_ID           = "minio"
      AWS_SECRET_ACCESS_KEY       = random_password.minio_root.result
      AWS_DEFAULT_REGION          = "us-east-1"
      MLFLOW_S3_ENDPOINT_URL      = "http://minio.experiment-tracking.svc.cluster.local:9000"
      MODEL_STORE_ENDPOINT        = "http://minio.experiment-tracking.svc.cluster.local:9000"
      MLFLOW_TRACKING_URI         = "http://mlflow.experiment-tracking.svc.cluster.local:5000"
      MLFLOW_EXPERIMENT_NAME      = "recsys-bst-ranking"
      MODEL_REGISTRY_POSTGRES_URI = "postgresql://mlflow:${random_password.mlflow_postgres.result}@postgres.experiment-tracking.svc.cluster.local:5432/mlflow"
      MODEL_STORE_BUCKET          = "recsys-model-store"
      MODEL_STORE_PREFIX          = "triton/bst"
      PROMOTION_MANIFEST_KEY      = "promotions/bst/latest.json"
      ICEBERG_CATALOG_NAME        = "recsys_features"
      ICEBERG_WAREHOUSE           = "s3a://recsys-offline-feature-store/warehouse"
      HUDI_ENABLED                = "true"
      HUDI_CATALOG_NAME           = "recsys_features"
      HUDI_WAREHOUSE              = "s3a://recsys-offline-feature-store/warehouse"
      HUDI_DATASET_TABLE          = "ml.bst_samples_native_v2"
      HUDI_CLEAN_HOURS_RETAINED   = "2160"
      HUDI_ZK_URL                 = "zookeeper.recsys-dataflow.svc.cluster.local"
      HUDI_ZK_PORT                = "2181"
      HUDI_ZK_BASE_PATH           = "/hudi/locks/bst_samples_native_v2"
      HUDI_ZK_LOCK_KEY            = "bst_samples_native_v2"
    }
    kserve-minio = {
      AWS_ACCESS_KEY_ID     = "minio"
      AWS_SECRET_ACCESS_KEY = random_password.minio_root.result
      AWS_DEFAULT_REGION    = "us-east-1"
      AWS_ENDPOINT_URL      = "http://minio.experiment-tracking.svc.cluster.local:9000"
      S3_ENDPOINT           = "minio.experiment-tracking.svc.cluster.local:9000"
      S3_USE_HTTPS          = "0"
    }
    gateway = {
      auth = local.gateway_htpasswd
    }
    agentregistry = {
      POSTGRES_DB                 = "agentregistry"
      POSTGRES_USER               = "agentregistry"
      POSTGRES_PASSWORD           = random_password.agentregistry_postgres.result
      AGENT_REGISTRY_DATABASE_URL = "postgresql://agentregistry:${random_password.agentregistry_postgres.result}@agentregistry-postgres.agentregistry.svc.cluster.local:5432/agentregistry?sslmode=disable"
    }
  }
}

resource "kubernetes_secret_v1" "centralized_recsys" {
  for_each = var.config.vault_legacy_source_secrets_enabled ? local.external_secret_payloads : {}

  metadata {
    name      = each.key
    namespace = local.external_secret_source_namespace
    labels = {
      "app.kubernetes.io/part-of" = "recsys-mlops"
      "recsys.ai/secret-scope"    = each.key
    }
  }

  data = each.value
  type = "Opaque"

  depends_on = [helm_release.external_secrets]
}

resource "null_resource" "recsys_external_secrets_ready" {
  triggers = {
    cluster_id         = var.cluster.id
    chart_revision     = local.external_secrets_chart_revision
    agent_gateway_auth = tostring(var.config.deploy_llm_inference && var.config.agent_gateway_auth_enabled)
    agent_registry     = tostring(var.config.deploy_agent_registry)
    langfuse           = tostring(var.config.deploy_langfuse)
    feature_rag_mcp    = tostring(var.config.deploy_llm_inference)
    recommendation_mcp = tostring(var.config.deploy_llm_inference)
    mcp_auth_versions  = local.mcp_auth_versions_sha256
    gateway_ui_auth    = "kagent,agentregistry"
  }

  provisioner "local-exec" {
    command     = <<-EOT
      set -euo pipefail
      while read -r namespace name; do
        kubectl wait --for=condition=Ready "externalsecret/$${name}" \
          -n "$${namespace}" --timeout=300s
        kubectl get "secret/$${name}" -n "$${namespace}" >/dev/null
      done <<'SECRETS'
      recsys-dataflow recsys-data-platform-secret
      observability recsys-data-platform-secret
      experiment-tracking recsys-mlflow-secrets
      kubeflow recsys-mlops-runtime
      kserve-triton-inference recsys-kserve-minio
      api-serving recsys-gateway-basic-auth
      api-serving recsys-rag-api
      observability recsys-gateway-basic-auth
      kagent recsys-gateway-basic-auth
      agentregistry recsys-gateway-basic-auth
      SECRETS

      if [[ "$${WAIT_LANGFUSE}" == "true" ]]; then
        kubectl wait --for=condition=Ready "externalsecret/recsys-gateway-basic-auth" \
          -n langfuse --timeout=300s
        kubectl get "secret/recsys-gateway-basic-auth" -n langfuse >/dev/null
      fi

      if [[ "$${WAIT_AGENT_GATEWAY}" == "true" ]]; then
        while read -r namespace name; do
          kubectl wait --for=condition=Ready "externalsecret/$${name}" \
            -n "$${namespace}" --timeout=300s
          kubectl get "secret/$${name}" -n "$${namespace}" >/dev/null
        done <<'AGENT_GATEWAY_SECRETS'
      kagent kagent-agent-gateway
      llm-inference agentgateway-api-keys
      AGENT_GATEWAY_SECRETS
      fi

      if [[ "$${WAIT_AGENT_REGISTRY}" == "true" ]]; then
        kubectl wait --for=condition=Ready "externalsecret/agentregistry-runtime" \
          -n agentregistry --timeout=300s
        kubectl get "secret/agentregistry-runtime" -n agentregistry >/dev/null
      fi

      if [[ "$${WAIT_MCP_AUTH}" == "true" ]]; then
        while IFS=$'\t' read -r namespace name; do
          [[ -n "$${namespace}" && -n "$${name}" ]] || continue
          kubectl wait --for=condition=Ready "externalsecret/$${name}" \
            -n "$${namespace}" --timeout=300s
          kubectl get "secret/$${name}" -n "$${namespace}" >/dev/null
        done <<< "$${MCP_AUTH_EXTERNAL_SECRETS}"
      fi
    EOT
    interpreter = ["/bin/bash", "-c"]
    environment = {
      WAIT_AGENT_GATEWAY  = tostring(var.config.deploy_llm_inference && var.config.agent_gateway_auth_enabled)
      WAIT_AGENT_REGISTRY = tostring(var.config.deploy_agent_registry)
      WAIT_MCP_AUTH       = tostring(var.config.deploy_llm_inference)
      MCP_AUTH_EXTERNAL_SECRETS = join("\n", [
        for target in local.mcp_auth_external_secret_targets :
        format("%s\t%s", target.namespace, target.name)
      ])
      WAIT_LANGFUSE = tostring(var.config.deploy_langfuse)
    }
  }

  depends_on = [helm_release.recsys_security]
}

# Run the same non-secret manifest validator used by CI before Terraform is
# allowed to mutate the security Helm release. On a purge commit the helper
# also verifies the separately approved retirement evidence and that no live
# workload, RemoteMCPServer, or SandboxAgent still references the old slot.
resource "null_resource" "mcp_auth_versions_valid" {
  triggers = {
    cluster_id        = var.cluster.id
    manifest_sha256   = local.mcp_auth_versions_sha256
    mcp_auth_enabled  = tostring(var.config.deploy_llm_inference)
    validator_sha256  = filesha256("${var.repo_root}/ops/security/mcp_auth_versions.py")
    purge_gate_sha256 = filesha256("${var.repo_root}/ops/security/validate_mcp_auth_terraform.sh")
    # External references can change between failed/retried applies even when
    # configuration hashes do not. Force the fail-closed live gate to run on
    # every apply before recsys-security can mutate. The kagent namespace is
    # deliberately retained across a generic feature-disable transaction.
    validation_attempt = timestamp()
  }

  provisioner "local-exec" {
    command     = "bash \"$MCP_AUTH_TERRAFORM_VALIDATOR\" \"$MCP_AUTH_VERSIONS_FILE\""
    interpreter = ["/bin/bash", "-c"]
    environment = {
      MCP_AUTH_TERRAFORM_VALIDATOR = "${var.repo_root}/ops/security/validate_mcp_auth_terraform.sh"
      MCP_AUTH_VERSIONS_FILE       = local.mcp_auth_versions_path
      MCP_AUTH_ENABLED             = tostring(var.config.deploy_llm_inference)
    }
  }

  lifecycle {
    # Run the replacement guard before destroying its prior instance when a
    # feature flag changes. This keeps recsys-security ordered behind the new
    # validation instead of opening a destroy-before-create gap.
    create_before_destroy = true
  }

  depends_on = [
    helm_release.external_secrets,
    # The namespace must exist before the live-state guard runs; generic
    # feature disable retains it so Jenkins-owned MCP objects stay observable.
    kubernetes_namespace.kagent,
  ]
}
