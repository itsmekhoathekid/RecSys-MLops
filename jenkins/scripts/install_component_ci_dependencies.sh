#!/usr/bin/env bash
set -euo pipefail

components=",${CHANGED_COMPONENTS:-},"
python_bin="${UV_PROJECT_ENVIRONMENT:?UV_PROJECT_ENVIRONMENT is required}/bin/python"

# The shared Jenkins environment intentionally stays small for data-only
# components. Training and KServe tests import the same ML stack baked into
# Dockerfile.training, so install that stack only when either component runs.
if [[ "${components}" == *,training,* || "${components}" == *,kserve,* || "${components}" == *,rollout,* ]]; then
  uv pip install --python "${python_bin}" --index-url https://download.pytorch.org/whl/cpu \
    torch

  uv pip install --python "${python_bin}" \
    "feast[redis]" \
    kfp-kubernetes \
    mlflow \
    onnx \
    onnxscript \
    psycopg-pool \
    "ray[default,train,tune]" \
    s3fs \
    tqdm
fi
