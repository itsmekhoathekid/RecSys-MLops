resource "google_service_account" "vault" {
  count = var.deploy_vault ? 1 : 0

  account_id   = "${var.name_prefix}-vault"
  display_name = "RecSys Vault auto-unseal"

  depends_on = [google_project_service.required]
}

resource "google_kms_key_ring" "vault" {
  count = var.deploy_vault ? 1 : 0

  name     = "${var.name_prefix}-vault"
  location = var.vault_kms_location
  project  = var.project_id

  depends_on = [google_project_service.required]
}

resource "google_kms_crypto_key" "vault_unseal" {
  count = var.deploy_vault ? 1 : 0

  name            = "vault-unseal"
  key_ring        = google_kms_key_ring.vault[0].id
  rotation_period = "7776000s"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key_iam_member" "vault_unseal" {
  count = var.deploy_vault ? 1 : 0

  crypto_key_id = google_kms_crypto_key.vault_unseal[0].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_service_account.vault[0].email}"
}

resource "google_kms_crypto_key_iam_member" "vault_unseal_metadata" {
  count = var.deploy_vault ? 1 : 0

  crypto_key_id = google_kms_crypto_key.vault_unseal[0].id
  role          = "roles/cloudkms.viewer"
  member        = "serviceAccount:${google_service_account.vault[0].email}"
}

resource "google_service_account_iam_member" "vault_workload_identity" {
  count = var.deploy_vault ? 1 : 0

  service_account_id = google_service_account.vault[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[vault/vault]"
}

resource "helm_release" "vault" {
  count = var.deploy_vault ? 1 : 0

  name             = "vault"
  repository       = "https://helm.releases.hashicorp.com"
  chart            = "vault"
  version          = var.vault_chart_version
  namespace        = "vault"
  create_namespace = true
  atomic           = false
  wait             = false
  timeout          = 900

  values = [
    templatefile("${path.module}/../../../configs/vault/values.yaml.tftpl", {
      gcp_project_id            = var.project_id
      vault_gcp_service_account = google_service_account.vault[0].email
      vault_kms_crypto_key      = google_kms_crypto_key.vault_unseal[0].name
      vault_kms_key_ring        = google_kms_key_ring.vault[0].name
      vault_kms_location        = var.vault_kms_location
      vault_replicas            = var.vault_replicas
      vault_storage_size        = var.vault_storage_size
    }),
  ]

  depends_on = [
    google_container_node_pool.cpu,
    google_kms_crypto_key_iam_member.vault_unseal,
    google_kms_crypto_key_iam_member.vault_unseal_metadata,
    google_service_account_iam_member.vault_workload_identity,
  ]
}

resource "kubernetes_cluster_role_binding_v1" "vault_token_reviewer" {
  count = var.deploy_vault ? 1 : 0

  metadata {
    name = "vault-token-reviewer"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "system:auth-delegator"
  }

  subject {
    kind      = "ServiceAccount"
    name      = "vault"
    namespace = "vault"
  }

  depends_on = [helm_release.vault]
}
