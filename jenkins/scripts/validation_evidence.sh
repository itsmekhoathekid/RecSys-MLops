#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

reports_dir="${REPORTS_DIR:-reports}/validation"
evidence_dir="${EVIDENCE_DIR:-docs/submission/rubic-final-coursework-(final-ml)/validation-verification}"
mkdir -p "${reports_dir}" "${evidence_dir}/screenshots"

python3 - "${evidence_dir}/README.md" <<'PY'
from __future__ import annotations

import glob
import json
import xml.etree.ElementTree as ET
from pathlib import Path
import sys

readme_path = Path(sys.argv[1])
coverage_rows: list[str] = []
for path in sorted(glob.glob("reports/coverage/*.xml")):
    root = ET.parse(path).getroot()
    rate = float(root.attrib.get("line-rate", "0")) * 100
    coverage_rows.append(f"| {Path(path).stem} | {rate:.2f}% |")

mutation_summary = Path("reports/validation/mutation-summary.md")
locust_summary = Path("reports/validation/load/locust-sla-summary.md")
mutation_json = Path("reports/validation/mutmut-cicd-stats.json")
mutation_score = "not run"
if mutation_json.exists():
    stats = json.loads(mutation_json.read_text(encoding="utf-8"))
    detected = stats["killed"] + stats.get("timeout", 0) + stats.get("caught_by_type_check", 0)
    live = stats["survived"] + stats.get("suspicious", 0) + stats.get("no_tests", 0)
    mutation_score = f"{100.0 if detected + live == 0 else detected / (detected + live) * 100.0:.2f}%"

content = [
    "# Validation & Verification Evidence",
    "",
    "## Coverage",
    "",
    "| Component | Line coverage |",
    "| --- | ---: |",
    *(coverage_rows or ["| not run | n/a |"]),
    "",
    "## Required Proof",
    "",
    "- Web API unit tests use `TestClient`, pytest fixtures, and mocked Redis/Triton services.",
    "- EP/BVA cases are visible in pytest IDs containing `equivalence-*` and `boundary-*`.",
    "- Property-based idempotency uses Hypothesis in `tests/unit/api_serving/test_validation_verification.py`.",
    f"- Mutation score: {mutation_score}.",
    "- Locust HTML SLA report: `locust-api.html` after `validation_load_test.sh` runs.",
    "",
    "## Commands",
    "",
    "```bash",
    "COVERAGE_MIN=90 UV_CACHE_DIR=.uv-cache bash jenkins/scripts/component_ci.sh api",
    "MUTATION_TARGETS='apps/api-serving/src/ranking.py apps/api-serving/src/online_features.py' MUTATION_MUTANT_NAMES='ranking.x_format_top_k* online_features.x_get_online_features*' UV_CACHE_DIR=.uv-cache bash jenkins/scripts/validation_mutation.sh",
    "RECSYS_LOAD_HOST=http://127.0.0.1:8088 UV_CACHE_DIR=.uv-cache bash jenkins/scripts/validation_load_test.sh",
    "bash jenkins/scripts/validation_evidence.sh",
    "```",
    "",
    "## Screenshot Checklist",
    "",
    "- `screenshots/coverage-api.png`: terminal coverage output showing `>90%`.",
    "- `screenshots/fixtures-mocks-web-api.png`: pytest output for `TestClient` + fixture/mock tests.",
    "- `screenshots/ep-bva-parametrize.png`: pytest output with `equivalence-*` and `boundary-*` case IDs.",
    "- `screenshots/mutation-score.png`: mutation summary showing score `>80%`.",
    "- `screenshots/property-idempotency.png`: Hypothesis idempotency test output.",
    "- `screenshots/locust-html-sla.png`: opened `locust-api.html` report.",
    "",
]
if mutation_summary.exists():
    content.extend(["## Mutation Summary", "", mutation_summary.read_text(encoding="utf-8"), ""])
if locust_summary.exists():
    content.extend(["## Locust Summary", "", locust_summary.read_text(encoding="utf-8"), ""])

readme_path.write_text("\n".join(content), encoding="utf-8")
print(f"Wrote {readme_path}")
PY
