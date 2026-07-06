#!/usr/bin/env bash
set -euo pipefail

package_path="${KFP_PACKAGE_PATH:-infra/kubeflow/compiled/bst_training_pipeline.yaml}"
training_image="${RECSYS_PIPELINE_IMAGE:?RECSYS_PIPELINE_IMAGE is required}"
ray_image="${RECSYS_RAY_IMAGE:-${training_image}}"
spark_image="${RECSYS_SPARK_IMAGE:?RECSYS_SPARK_IMAGE is required}"
kfp_endpoint="${KFP_ENDPOINT:-http://ml-pipeline.kubeflow.svc.cluster.local:8888}"
pipeline_name="${KFP_PIPELINE_NAME:-recsys-bst-feature-train-evaluate}"
pipeline_version_name="${KFP_PIPELINE_VERSION_NAME:-}"
upload_package="${KFP_UPLOAD_PACKAGE:-1}"

python_cmd=()
if [[ -n "${KFP_CICD_PYTHON:-}" ]]; then
  python_cmd=("${KFP_CICD_PYTHON}")
elif command -v uv >/dev/null 2>&1; then
  python_cmd=(uv run python)
else
  python_cmd=(python)
fi

export PYTHONPATH="${PYTHONPATH:-apps/ml-system/src:apps/data-platform/src}"
export RECSYS_PIPELINE_IMAGE="${training_image}"
export RECSYS_RAY_IMAGE="${ray_image}"
export RECSYS_SPARK_IMAGE="${spark_image}"

echo "Compiling Kubeflow package with training image: ${training_image}"
echo "Compiling Kubeflow package with Spark image: ${spark_image}"
"${python_cmd[@]}" apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py --package-path "${package_path}"

"${python_cmd[@]}" apps/ml-system/src/kubeflow/validate_pipeline_package.py \
  --package-path "${package_path}" \
  --required-image "${training_image}" \
  --required-image "${ray_image}" \
  --required-image "${spark_image}" \
  --forbidden-token ":local"

if [[ "${upload_package}" == "0" || "${upload_package}" == "false" ]]; then
  echo "Skipping Kubeflow package upload because KFP_UPLOAD_PACKAGE=${upload_package}."
  exit 0
fi

upload_args=(
  --host "${kfp_endpoint}"
  --package-path "${package_path}"
  --pipeline-name "${pipeline_name}"
)

if [[ -n "${pipeline_version_name}" ]]; then
  upload_args+=(--pipeline-version-name "${pipeline_version_name}")
fi

"${python_cmd[@]}" apps/ml-system/src/kubeflow/upload_pipeline_package.py "${upload_args[@]}"
