#!/usr/bin/env bash
set -euo pipefail

package_path="${KFP_PACKAGE_PATH:-pipelines/kubeflow/compiled/bst_training_pipeline.yaml}"
training_image="${RECSYS_PIPELINE_IMAGE:?RECSYS_PIPELINE_IMAGE is required}"
ray_image="${RECSYS_RAY_IMAGE:-${training_image}}"
spark_image="${RECSYS_SPARK_ML_IMAGE:?RECSYS_SPARK_ML_IMAGE is required}"

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

export PYTHONPATH="${PYTHONPATH:-apps/ml-system/src:apps/data-platform/src}"
export RECSYS_PIPELINE_IMAGE="${training_image}"
export RECSYS_RAY_IMAGE="${ray_image}"
export RECSYS_SPARK_ML_IMAGE="${spark_image}"

echo "Compiling Kubeflow package with training image: ${training_image}"
echo "Compiling Kubeflow package with Spark image: ${spark_image}"
"${python_cmd[@]}" apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py \
  --package-path "${package_path}"

"${python_cmd[@]}" apps/ml-system/src/kubeflow/validate_pipeline_package.py \
  --package-path "${package_path}" \
  --required-image "${training_image}" \
  --required-image "${ray_image}" \
  --required-image "${spark_image}" \
  --forbidden-token ":local"
