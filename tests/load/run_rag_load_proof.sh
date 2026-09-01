#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
port="${RECSYS_RAG_LOAD_PORT:-8092}"
duration="${RECSYS_RAG_LOAD_DURATION:-10s}"
report_dir="$repo_root/docs/submission/rubic-final-coursework-(final-ml)/validation-verification/rag-load"
report_html="$report_dir/locust-rag.html"
csv_prefix="$report_dir/locust-rag"
server_log="${TMPDIR:-/tmp}/recsys-rag-load-server-${port}.log"
uv_cache_dir="${UV_CACHE_DIR:-$repo_root/.uv-cache}"

mkdir -p "$report_dir"
cd "$repo_root"

UV_CACHE_DIR="$uv_cache_dir" \
PYTHONPATH="apps/api-serving/shared/src:apps/api-serving/rag-api/src:apps/data-platform/rag-runtime/src" \
    RECSYS_OTEL_ENABLED=0 \
    uv run uvicorn rag_load_app:app \
    --app-dir tests/load \
    --host 127.0.0.1 \
    --port "$port" >"$server_log" 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT

for _ in {1..50}; do
    if curl --fail --silent "http://127.0.0.1:$port/healthz" >/dev/null; then
        break
    fi
    sleep 0.1
done
curl --fail --silent "http://127.0.0.1:$port/healthz" >/dev/null

UV_CACHE_DIR="$uv_cache_dir" RECSYS_LOAD_TARGET=rag uv run locust \
    -f tests/load/locustfile_serving.py \
    --headless \
    --users 4 \
    --spawn-rate 2 \
    --run-time "$duration" \
    --host "http://127.0.0.1:$port" \
    --html "$report_html" \
    --csv "$csv_prefix" \
    --only-summary

awk -F, '
NR == 1 {
    for (i = 1; i <= NF; i++) header[$i] = i
    next
}
$2 == "Aggregated" {
    found = 1
    requests = $(header["Request Count"])
    failures = $(header["Failure Count"])
    rps = $(header["Requests/s"])
    p95 = $(header["95%"])
    printf "RAG SLA: requests=%d failures=%d rps=%.2f p95_ms=%.2f\n", requests, failures, rps, p95
    if (requests < 1 || failures != 0 || rps < 5 || p95 >= 1000) exit 1
}
END {
    if (!found) exit 2
}
' "${csv_prefix}_stats.csv"

printf 'RAG Locust proof: %s\n' "$report_html"
