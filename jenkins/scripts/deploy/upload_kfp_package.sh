#!/usr/bin/env bash
set -euo pipefail

package_path="${KFP_PACKAGE_PATH:-pipelines/kubeflow/compiled/bst_training_pipeline.yaml}"
kfp_endpoint="${KFP_ENDPOINT:-http://ml-pipeline.kubeflow.svc.cluster.local:8888}"
pipeline_name="${KFP_PIPELINE_NAME:-recsys-bst-feature-train-evaluate}"
pipeline_version_name="${KFP_PIPELINE_VERSION_NAME:-}"
[[ -s "${package_path}" ]] || {
  printf 'compiled Kubeflow package does not exist: %s\n' "${package_path}" >&2
  exit 2
}

python_cmd=()
if [[ -n "${KFP_CICD_PYTHON:-}" ]]; then
  python_cmd=("${KFP_CICD_PYTHON}")
elif [[ -n "${UV_PROJECT_ENVIRONMENT:-}" && -x "${UV_PROJECT_ENVIRONMENT}/bin/python" ]]; then
  python_cmd=("${UV_PROJECT_ENVIRONMENT}/bin/python")
elif command -v uv >/dev/null 2>&1; then
  python_cmd=(uv run python)
else
  python_cmd=(python)
fi

upload_args=(
  --host "${kfp_endpoint}"
  --package-path "${package_path}"
  --pipeline-name "${pipeline_name}"
)
[[ -n "${pipeline_version_name}" ]] \
  && upload_args+=(--pipeline-version-name "${pipeline_version_name}")
[[ -n "${KFP_UPLOAD_RESULT_PATH:-}" ]] \
  && upload_args+=(--output-json "${KFP_UPLOAD_RESULT_PATH}")

PYTHONPATH="${PYTHONPATH:-apps/ml-system/src:apps/data-platform/src}" \
  "${python_cmd[@]}" apps/ml-system/src/kubeflow/upload_pipeline_package.py \
  "${upload_args[@]}"
