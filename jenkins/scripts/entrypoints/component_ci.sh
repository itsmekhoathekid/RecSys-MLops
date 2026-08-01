#!/usr/bin/env bash
set -euo pipefail

source jenkins/scripts/lib/common.sh

component="${1:?component is required}"
coverage_min="${COVERAGE_MIN:-90}"
reports_dir="${REPORTS_DIR:-reports}"
mkdir -p "${reports_dir}/junit" "${reports_dir}/coverage"
ci_profile="$(python3 jenkins/python/configuration.py component-profile "${component}")"
ci_environment="${CI_TMP_ROOT:?CI_TMP_ROOT is required}/envs/${ci_profile}"
ci_python="${ci_environment}/bin/python"
[[ -x "${ci_python}" ]] || {
  echo "Locked CI environment is missing for ${component}: ${ci_environment}" >&2
  exit 2
}
export UV_PROJECT_ENVIRONMENT="${ci_environment}"
export PYSPARK_PYTHON="${ci_python}"
export PYSPARK_DRIVER_PYTHON="${ci_python}"

source jenkins/scripts/ci/runtime.sh
source jenkins/scripts/ci/data.sh
source jenkins/scripts/ci/ml.sh
source jenkins/scripts/ci/serving.sh
source jenkins/scripts/ci/demo.sh
source jenkins/scripts/ci/analytics.sh
source jenkins/scripts/ci/dispatch.sh

run_component_ci "${component}"
migration_args=(--component "${component}")
if [[ -n "${CI_BASE_REF:-}" ]]; then
  migration_args+=(--base-ref "${CI_BASE_REF}")
fi
python3 -m jenkins.python.migration_policy "${migration_args[@]}"
