#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

base_ref="${MUTATION_BASE_REF:-origin/main}"
threshold="${MUTATION_SCORE_MIN:-80}"
reports_dir="${REPORTS_DIR:-reports}/validation"
evidence_dir="${EVIDENCE_DIR:-docs/submission/rubic-final-coursework-(final-ml)/validation-verification}"
mkdir -p "${reports_dir}" "${evidence_dir}"

test_selection_input="${MUTATION_TEST_SELECTION:-tests/unit/api_serving/test_serving.py tests/unit/api_serving/test_validation_verification.py::test_property_based_recommendation_idempotency_for_deterministic_prediction}"
mutation_test_selection=()
while IFS= read -r test_path; do
  mutation_test_selection+=("${test_path}")
done < <(printf '%s\n' "${test_selection_input}" | tr ', ' '\n' | sed '/^$/d')

mutant_names_input="${MUTATION_MUTANT_NAMES:-}"
mutation_mutant_names=()
while IFS= read -r mutant_name; do
  mutation_mutant_names+=("${mutant_name}")
done < <(printf '%s\n' "${mutant_names_input}" | tr ', ' '\n' | sed '/^$/d')

target_input="${MUTATION_TARGETS:-}"
if [[ -z "${target_input}" ]]; then
  if git rev-parse --verify "${base_ref}" >/dev/null 2>&1; then
    target_input="$(git diff --name-only "${base_ref}...HEAD" || true)"
  else
    target_input="$(git diff --name-only HEAD~1...HEAD || true)"
  fi
fi

mutation_targets=()
while IFS= read -r target; do
  mutation_targets+=("${target}")
done < <(
  printf '%s\n' "${target_input}" \
    | tr ', ' '\n' \
    | sed '/^$/d' \
    | awk '
      /^apps\/api-serving\/src\/.*\.py$/ { print; next }
      /^apps\/data-platform\/src\/.*\.py$/ { print; next }
      /^apps\/data-platform\/data-generator\/src\/.*\.py$/ { print; next }
      /^apps\/ml-system\/src\/.*\.py$/ { print; next }
      /^jenkins\/scripts\/.*\.py$/ { print; next }
    ' \
    | sort -u
)

if [[ "${#mutation_targets[@]}" -eq 0 ]]; then
  cat >"${reports_dir}/mutation-summary.md" <<'EOF'
# Mutation Testing

No changed Python source files matched the mutation allowlist, so mutation testing
was skipped. Set MUTATION_TARGETS to run a focused rubric proof target manually.
EOF
  cp "${reports_dir}/mutation-summary.md" "${evidence_dir}/mutation-summary.md"
  echo "No changed Python source files to mutate."
  exit 0
fi

mutation_root=""
relative_targets=()
for target in "${mutation_targets[@]}"; do
  case "${target}" in
    apps/api-serving/src/*)
      root="apps/api-serving/src"
      relative="${target#apps/api-serving/src/}"
      ;;
    apps/data-platform/src/*)
      root="apps/data-platform/src"
      relative="${target#apps/data-platform/src/}"
      ;;
    apps/data-platform/data-generator/src/*)
      root="apps/data-platform/data-generator/src"
      relative="${target#apps/data-platform/data-generator/src/}"
      ;;
    apps/ml-system/src/*)
      root="apps/ml-system/src"
      relative="${target#apps/ml-system/src/}"
      ;;
    jenkins/scripts/*)
      root="jenkins/scripts"
      relative="${target#jenkins/scripts/}"
      ;;
    *)
      echo "Unsupported mutation target: ${target}" >&2
      exit 2
      ;;
  esac
  if [[ -z "${mutation_root}" ]]; then
    mutation_root="${root}"
  elif [[ "${mutation_root}" != "${root}" ]]; then
    echo "Mutation targets span multiple source roots. Run this script once per source root." >&2
    exit 2
  fi
  relative_targets+=("${relative}")
done

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/recsys-mutmut.XXXXXX")"
cleanup() {
  rm -rf "${tmpdir}"
}
trap cleanup EXIT

find "${repo_root}/${mutation_root}" -mindepth 1 -maxdepth 1 -exec ln -s {} "${tmpdir}/" \;
ln -s "${repo_root}/tests" "${tmpdir}/tests"
ln -s "${repo_root}/apps" "${tmpdir}/apps"
ln -s "${repo_root}/jenkins" "${tmpdir}/jenkins"
ln -s "${repo_root}/infra" "${tmpdir}/infra"
ln -s "${repo_root}/configs" "${tmpdir}/configs"

{
  cat <<'EOF'
[tool.mutmut]
source_paths = [
EOF
  for target in "${relative_targets[@]}"; do
    printf '  "%s",\n' "${target}"
  done
  cat <<'EOF'
]
pytest_add_cli_args = ["-q"]
pytest_add_cli_args_test_selection = [
EOF
  for test_path in "${mutation_test_selection[@]}"; do
    printf '  "%s",\n' "${test_path}"
  done
  cat <<'EOF'
]
mutate_only_covered_lines = true
use_setproctitle = false
only_mutate = [
EOF
  for target in "${relative_targets[@]}"; do
    printf '  "%s",\n' "${target}"
  done
  cat <<'EOF'
]
EOF
} > "${tmpdir}/pyproject.toml"

export PYTHONPATH=".:apps/api-serving/src:apps/ml-system/src:apps/data-platform/src:apps/data-platform/data-generator/src:jenkins/scripts"
export UV_CACHE_DIR="${UV_CACHE_DIR:-${repo_root}/.uv-cache}"

pushd "${tmpdir}" >/dev/null
mutmut_run_args=(run --max-children "${MUTMUT_MAX_CHILDREN:-2}")
if [[ "${#mutation_mutant_names[@]}" -gt 0 ]]; then
  mutmut_run_args+=("${mutation_mutant_names[@]}")
fi
"${repo_root}/.venv/bin/mutmut" "${mutmut_run_args[@]}" | tee "${repo_root}/${reports_dir}/mutation-run.log"
"${repo_root}/.venv/bin/mutmut" export-cicd-stats | tee "${repo_root}/${reports_dir}/mutation-export.log"
"${repo_root}/.venv/bin/mutmut" results --all true > "${repo_root}/${reports_dir}/mutation-results.txt"
popd >/dev/null

cp "${tmpdir}/mutants/mutmut-cicd-stats.json" "${reports_dir}/mutmut-cicd-stats.json"

summary_args=(
  "${reports_dir}/mutmut-cicd-stats.json"
  "${threshold}"
  "${reports_dir}/mutation-summary.md"
  "${#mutation_targets[@]}"
  "${mutation_targets[@]}"
)
if [[ "${#mutation_mutant_names[@]}" -gt 0 ]]; then
  summary_args+=("${mutation_mutant_names[@]}")
fi
python3 - "${summary_args[@]}" <<'PY'
import json
import sys
from pathlib import Path

stats_path = Path(sys.argv[1])
threshold = float(sys.argv[2])
summary_path = Path(sys.argv[3])
target_count = int(sys.argv[4])
targets = sys.argv[5 : 5 + target_count]
mutant_filters = sys.argv[5 + target_count :]
stats = json.loads(stats_path.read_text(encoding="utf-8"))
detected = stats["killed"] + stats.get("timeout", 0) + stats.get("caught_by_type_check", 0)
live = stats["survived"] + stats.get("suspicious", 0) + stats.get("no_tests", 0)
score = 100.0 if detected + live == 0 else detected / (detected + live) * 100.0
summary = [
    "# Mutation Testing",
    "",
    f"- Mutation score: {score:.2f}%",
    f"- Gate: > {threshold:.2f}%",
    f"- Killed: {stats['killed']}",
    f"- Survived: {stats['survived']}",
    f"- Timeout: {stats.get('timeout', 0)}",
    f"- Suspicious: {stats.get('suspicious', 0)}",
    f"- No tests: {stats.get('no_tests', 0)}",
    f"- Targets: {', '.join(targets)}",
    f"- Mutant filters: {', '.join(mutant_filters) or 'all mutants in target files'}",
    "",
]
summary_path.write_text("\n".join(summary), encoding="utf-8")
print("\n".join(summary))
if score <= threshold:
    raise SystemExit(f"Mutation score {score:.2f}% is not > {threshold:.2f}%")
PY

cp "${reports_dir}/mutation-summary.md" "${evidence_dir}/mutation-summary.md"
cp "${reports_dir}/mutation-results.txt" "${evidence_dir}/mutation-results.txt"
