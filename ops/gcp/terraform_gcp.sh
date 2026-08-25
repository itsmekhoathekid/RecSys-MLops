#!/usr/bin/env bash
set -Eeuo pipefail

project_id="${GCP_PROJECT_ID:-recsys-mlops-506406}"
expected_account="${GCP_ACCOUNT:-tuilaweo@gmail.com}"
credential_file="${GCP_TERRAFORM_CREDENTIAL_FILE:-${HOME}/.config/gcloud/legacy_credentials/${expected_account}/adc.json}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tf_data_dir="${GCP_TERRAFORM_DATA_DIR:-${repo_root}/infra/terraform/gcp/.terraform/recsys-mlops-506406-gcs}"

active_account="$(gcloud auth list --filter=status:ACTIVE --format='value(account)')"
active_project="$(gcloud config get-value project 2>/dev/null)"
[[ "${active_account}" == "${expected_account}" ]] || {
  echo "Refusing Terraform: active account ${active_account:-<none>} is not ${expected_account}" >&2
  exit 2
}
[[ "${active_project}" == "${project_id}" ]] || {
  echo "Refusing Terraform: active project ${active_project:-<none>} is not ${project_id}" >&2
  exit 2
}
[[ -f "${credential_file}" ]] || {
  echo "Terraform credential file is missing: ${credential_file}" >&2
  exit 2
}

export GOOGLE_APPLICATION_CREDENTIALS="${credential_file}"
export TF_DATA_DIR="${tf_data_dir}"
exec terraform "$@"
