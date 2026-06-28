#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

reports_dir="${REPORTS_DIR:-reports}/validation/load"
evidence_dir="${EVIDENCE_DIR:-docs/submission/rubic-final-coursework-(final-ml)/validation-verification}"
mkdir -p "${reports_dir}" "${evidence_dir}"

host="${RECSYS_LOAD_HOST:-http://127.0.0.1:${FASTAPI_PORT:-8088}}"
users="${RECSYS_LOAD_USERS:-8}"
spawn_rate="${RECSYS_LOAD_SPAWN_RATE:-2}"
duration="${RECSYS_LOAD_DURATION:-1m}"
html_report="${reports_dir}/locust-api.html"
csv_prefix="${reports_dir}/locust-api"

export UV_CACHE_DIR="${UV_CACHE_DIR:-${repo_root}/.uv-cache}"
uv run locust \
  -f tests/load/locustfile_serving.py \
  --headless \
  --host "${host}" \
  --users "${users}" \
  --spawn-rate "${spawn_rate}" \
  --run-time "${duration}" \
  --html "${html_report}" \
  --csv "${csv_prefix}" \
  --only-summary

python3 - "${csv_prefix}_stats.csv" "${reports_dir}/locust-sla-summary.md" <<'PY'
import csv
import sys
from pathlib import Path

stats_path = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
rows = list(csv.DictReader(stats_path.open(newline="", encoding="utf-8")))
aggregate = next((row for row in rows if row.get("Name") == "Aggregated"), rows[-1])
request_count = int(float(aggregate["Request Count"]))
failure_count = int(float(aggregate["Failure Count"]))
rps = float(aggregate["Requests/s"])
p95 = float(aggregate["95%"])
failure_rate = 0.0 if request_count == 0 else failure_count / request_count * 100.0
passed = failure_count == 0 and p95 < 1000.0 and rps >= 5.0
summary = [
    "# Locust Web API SLA",
    "",
    f"- Host: {aggregate.get('Name', 'Aggregated')}",
    f"- Requests: {request_count}",
    f"- Failures: {failure_count}",
    f"- Failure rate: {failure_rate:.2f}%",
    f"- Throughput: {rps:.2f} req/s",
    f"- p95 latency: {p95:.2f} ms",
    "- SLA: failure rate 0%, p95 < 1000 ms, throughput >= 5 req/s",
    f"- Result: {'PASS' if passed else 'FAIL'}",
    "",
]
summary_path.write_text("\n".join(summary), encoding="utf-8")
print("\n".join(summary))
if not passed:
    raise SystemExit("Locust SLA failed")
PY

cp "${html_report}" "${evidence_dir}/locust-api.html"
cp "${reports_dir}/locust-sla-summary.md" "${evidence_dir}/locust-sla-summary.md"
