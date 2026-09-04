#!/usr/bin/env bash
set -euo pipefail

ci_config_venv="${CI_TMP_ROOT:?CI_TMP_ROOT is required}/ci-config-venv"
uv venv "${ci_config_venv}"
uv pip install --python "${ci_config_venv}/bin/python" pytest pyyaml
"${ci_config_venv}/bin/python" -m pytest \
  tests/unit/jenkins \
  tests/unit/observability \
  tests/contract/test_langfuse_infrastructure_contracts.py \
  -q \
  --junitxml=reports/junit/ci-config.xml

python3 -m compileall -q jenkins/python jenkins/scripts
find jenkins/scripts ops -type f -name '*.sh' -print0 | xargs -0 bash -n

for chart_file in infra/helm/*/Chart.yaml; do
  chart_dir="$(dirname "${chart_file}")"
  if [[ "${chart_dir}" == "infra/helm/recsys-rag-data" ]]; then
    helm lint "${chart_dir}" -f "${chart_dir}/values-gcp.yaml"
    helm template validation "${chart_dir}" \
      -f "${chart_dir}/values-gcp.yaml" \
      --set job.runId=ci-validation >/dev/null
  elif [[ -f "${chart_dir}/values-gcp.yaml" ]]; then
    helm lint "${chart_dir}" -f "${chart_dir}/values-gcp.yaml"
    helm template validation "${chart_dir}" \
      -f "${chart_dir}/values-gcp.yaml" >/dev/null
  elif [[ "${chart_dir}" == "infra/helm/recsys-ci" ]]; then
    helm lint "${chart_dir}" -f "${chart_dir}/values-gke.yaml"
    helm template validation "${chart_dir}" \
      -f "${chart_dir}/values-gke.yaml" >/dev/null
  else
    helm lint "${chart_dir}"
    helm template validation "${chart_dir}" >/dev/null
  fi
done
