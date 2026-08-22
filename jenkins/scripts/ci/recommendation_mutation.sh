#!/usr/bin/env bash
set -euo pipefail

project="apps/agentic/recsys-recommendation-mcp"
environment="${1:?locked recommendation CI environment is required}"
case "${environment}" in
  /*) ;;
  *) environment="$(pwd)/${environment}" ;;
esac

(
  cd "${project}"
  "${environment}/bin/mutmut" run --max-children "${MUTMUT_MAX_CHILDREN:-2}"
  "${environment}/bin/mutmut" export-cicd-stats
)

python3 - "${project}/mutants/mutmut-cicd-stats.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
killed = payload["killed"]
total = payload["total"]
score = 100.0 * killed / total if total else 0.0
print(f"recommendation mutation score: {score:.2f}% ({killed}/{total})")
if score < 80.0:
    raise SystemExit("recommendation mutation score is below 80%")
PY
