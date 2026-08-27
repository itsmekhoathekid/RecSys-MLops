#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."

mode="${1:-status}"
hold_seconds="${AGENTIC_CAPTURE_HOLD_SECONDS:-45}"
wait_seconds="${AGENTIC_CAPTURE_WAIT_SECONDS:-300}"
decision_grace_seconds="${AGENTIC_CAPTURE_DECISION_GRACE_SECONDS:-60}"
fallback_wait_seconds="${AGENTIC_CAPTURE_FALLBACK_WAIT_SECONDS:-420}"
confirm_prod="${AGENTIC_CAPTURE_CONFIRM_PROD:-}"
reallocate="${AGENTIC_CAPTURE_REALLOCATE:-0}"
run_fallback="${AGENTIC_CAPTURE_FALLBACK:-1}"
chunk_id="${AGENTIC_SMOKE_CHUNK_ID:-800096:review:rev_800096_01:0}"
agent_user_id="${AGENTIC_SMOKE_USER_ID:-1001}"
rag_duration="${AGENTIC_CAPTURE_RAG_DURATION_SECONDS:-150}"
rag_rps="${AGENTIC_CAPTURE_RAG_RPS:-16}"
mcp_duration="${AGENTIC_CAPTURE_MCP_DURATION_SECONDS:-120}"
mcp_rps="${AGENTIC_CAPTURE_MCP_RPS:-20}"
agent_requests="${AGENTIC_CAPTURE_AGENT_REQUESTS:-1}"
agent_concurrency="${AGENTIC_CAPTURE_AGENT_CONCURRENCY:-1}"
agent_duration="${AGENTIC_CAPTURE_AGENT_DURATION_SECONDS:-180}"
a2a_port="${AGENTIC_CAPTURE_A2A_PORT:-18096}"

mkdir -p reports/agentic

load_pid=""
port_forward_pid=""
patched_scaledobject=""
original_scaler_address=""
capacity_prepared=false
original_qwen_replicas=""
original_ui_replicas=""

usage() {
  cat <<'EOF'
Usage: ops/validation/agentic_autoscale_capture.sh status|rag|mcp|worker|all

Load modes require AGENTIC_CAPTURE_CONFIRM_PROD=yes.
Set AGENTIC_CAPTURE_REALLOCATE=1 on the current quota-capped two-node cluster;
the script temporarily changes qwen35-gguf 2->1 and kagent-ui 1->0, then
restores both through a trap.

Useful overrides:
  AGENTIC_CAPTURE_HOLD_SECONDS=60
  AGENTIC_CAPTURE_FALLBACK=0
  AGENTIC_SMOKE_CHUNK_ID=<active chunk id>
  AGENTIC_CAPTURE_RAG_RPS=16
  AGENTIC_CAPTURE_MCP_RPS=20
  AGENTIC_CAPTURE_AGENT_REQUESTS=1
  AGENTIC_CAPTURE_AGENT_CONCURRENCY=1
  AGENTIC_CAPTURE_AGENT_DURATION_SECONDS=180
  # Optional admission/backpressure stress profile:
  AGENTIC_CAPTURE_AGENT_REQUESTS=20 AGENTIC_CAPTURE_AGENT_CONCURRENCY=20
EOF
}

restore_scaler() {
  if [[ -n "${patched_scaledobject}" && -n "${original_scaler_address}" ]]; then
    kubectl -n kagent patch scaledobject "${patched_scaledobject}" --type=json \
      -p "[{\"op\":\"replace\",\"path\":\"/spec/triggers/0/metadata/serverAddress\",\"value\":\"${original_scaler_address}\"}]" \
      >/dev/null || true
    patched_scaledobject=""
    original_scaler_address=""
  fi
}

restore_capacity() {
  if [[ "${capacity_prepared}" != "true" ]]; then
    return
  fi
  echo "Restoring qwen35-gguf=${original_qwen_replicas} and kagent-ui=${original_ui_replicas}."
  kubectl -n llm-inference scale deployment/qwen35-gguf \
    --replicas="${original_qwen_replicas}" >/dev/null || true
  kubectl -n kagent scale deployment/kagent-ui \
    --replicas="${original_ui_replicas}" >/dev/null || true
  kubectl -n llm-inference rollout status deployment/qwen35-gguf \
    --timeout=5m >/dev/null || true
  kubectl -n kagent rollout status deployment/kagent-ui \
    --timeout=5m >/dev/null || true
  capacity_prepared=false
}

cleanup() {
  set +e
  [[ -n "${load_pid}" ]] && kill "${load_pid}" >/dev/null 2>&1
  [[ -n "${port_forward_pid}" ]] && kill "${port_forward_pid}" >/dev/null 2>&1
  restore_scaler
  restore_capacity
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_for_deployment() {
  local namespace="$1"
  local deployment="$2"
  local expected="$3"
  local comparison="${4:-at-least}"
  local attempts=$((wait_seconds / 5))
  local desired available

  for _ in $(seq 1 "${attempts}"); do
    desired="$(kubectl -n "${namespace}" get deployment "${deployment}" \
      -o jsonpath='{.spec.replicas}')"
    available="$(kubectl -n "${namespace}" get deployment "${deployment}" \
      -o jsonpath='{.status.availableReplicas}')"
    echo "$(date '+%H:%M:%S') ${namespace}/${deployment} desired=${desired:-0} available=${available:-0}"
    if [[ "${comparison}" == "exact" ]]; then
      [[ "${desired:-0}" == "${expected}" && "${available:-0}" == "${expected}" ]] && return 0
    elif [[ "${desired:-0}" -ge "${expected}" && "${available:-0}" -ge "${expected}" ]]; then
      return 0
    fi
    sleep 5
  done
  echo "${namespace}/${deployment} did not reach ${comparison} ${expected} replicas." >&2
  return 1
}

wait_for_scale_with_load() {
  local namespace="$1"
  local deployment="$2"
  local expected="$3"
  local stderr_log="$4"
  local attempts=$((wait_seconds / 5))
  local desired available load_rc load_finished=false
  local grace_deadline=0

  for _ in $(seq 1 "${attempts}"); do
    desired="$(kubectl -n "${namespace}" get deployment "${deployment}" \
      -o jsonpath='{.spec.replicas}')"
    available="$(kubectl -n "${namespace}" get deployment "${deployment}" \
      -o jsonpath='{.status.availableReplicas}')"
    echo "$(date '+%H:%M:%S') ${namespace}/${deployment} desired=${desired:-0} available=${available:-0}"
    if [[ "${desired:-0}" -ge "${expected}" && "${available:-0}" -ge "${expected}" ]]; then
      return 0
    fi
    if [[ -n "${load_pid}" ]] && ! kill -0 "${load_pid}" >/dev/null 2>&1; then
      if wait "${load_pid}"; then
        load_rc=0
      else
        load_rc=$?
      fi
      load_pid=""
      if [[ "${load_rc}" -ne 0 ]]; then
        [[ -s "${stderr_log}" ]] && cat "${stderr_log}" >&2
        echo "Load generator exited with code ${load_rc} before ${namespace}/${deployment} reached ${expected} replicas." >&2
        return 1
      fi
      load_finished=true
      grace_deadline=$((SECONDS + decision_grace_seconds))
      echo "Load completed; allowing ${decision_grace_seconds}s for the KEDA polling/rate window."
    fi
    if [[ "${load_finished}" == "true" && "${SECONDS}" -ge "${grace_deadline}" ]]; then
      [[ -s "${stderr_log}" ]] && cat "${stderr_log}" >&2
      echo "KEDA did not scale ${namespace}/${deployment} within the post-load grace window." >&2
      return 1
    fi
    sleep 5
  done
  [[ -s "${stderr_log}" ]] && cat "${stderr_log}" >&2
  echo "${namespace}/${deployment} did not reach ${expected} replicas while load was active." >&2
  return 1
}

wait_for_load_completion() {
  if [[ -n "${load_pid}" ]]; then
    wait "${load_pid}"
    load_pid=""
  fi
}

capture_pause() {
  local title="$1"
  echo
  echo "================================================================"
  echo "CAPTURE NOW: ${title}"
  echo "K9s refreshes automatically; capture HPA, Deployment and pods."
  echo "Holding this state for ${hold_seconds} seconds."
  echo "================================================================"
  sleep "${hold_seconds}"
}

show_status() {
  echo "Context: $(kubectl config current-context)"
  kubectl get pods -A --field-selector=status.phase=Pending -o wide
  kubectl -n api-serving get deployment recsys-rag-api
  kubectl -n api-serving get hpa keda-hpa-recsys-rag-api
  kubectl -n kagent get deployment \
    recsys-feature-rag-mcp recsys-context-sandbox-pool
  kubectl -n kagent get hpa \
    keda-hpa-recsys-feature-rag-mcp \
    keda-hpa-recsys-context-sandbox-pool
  kubectl -n kagent get scaledobject \
    recsys-feature-rag-mcp recsys-context-sandbox-pool
  kubectl -n kagent get workerpool recsys-context-sandbox-pool
  kubectl -n kagent get sandboxagent recsys-context-agent-sandbox
  kubectl -n kagent get remotemcpserver recsys-feature-rag-mcp
}

prepare_capacity() {
  if [[ "${reallocate}" != "1" || "${capacity_prepared}" == "true" ]]; then
    return
  fi
  original_qwen_replicas="$(kubectl -n llm-inference get deployment qwen35-gguf \
    -o jsonpath='{.spec.replicas}')"
  original_ui_replicas="$(kubectl -n kagent get deployment kagent-ui \
    -o jsonpath='{.spec.replicas}')"
  capacity_prepared=true
  echo "Temporarily reallocating capacity: qwen35-gguf ${original_qwen_replicas}->1, kagent-ui ${original_ui_replicas}->0."
  kubectl -n llm-inference scale deployment/qwen35-gguf --replicas=1
  kubectl -n kagent scale deployment/kagent-ui --replicas=0
  kubectl -n llm-inference rollout status deployment/qwen35-gguf --timeout=5m
}

prove_fallback() {
  local scaledobject="$1"
  local deployment="$2"
  local expected="$3"
  local restore_grace_seconds="${4:-0}"

  patched_scaledobject="${scaledobject}"
  original_scaler_address="$(kubectl -n kagent get scaledobject "${scaledobject}" \
    -o jsonpath='{.spec.triggers[0].metadata.serverAddress}')"
  kubectl -n kagent patch scaledobject "${scaledobject}" --type=json \
    -p='[{"op":"replace","path":"/spec/triggers/0/metadata/serverAddress","value":"http://unreachable.invalid:9090"}]' \
    >/dev/null

  local attempts=$((fallback_wait_seconds / 5))
  local fallback available
  for _ in $(seq 1 "${attempts}"); do
    fallback="$(kubectl -n kagent get scaledobject "${scaledobject}" \
      -o jsonpath='{.status.conditions[?(@.type=="Fallback")].status}')"
    available="$(kubectl -n kagent get deployment "${deployment}" \
      -o jsonpath='{.status.availableReplicas}')"
    echo "$(date '+%H:%M:%S') ${scaledobject} fallback=${fallback:-False} available=${available:-0}"
    if [[ "${fallback}" == "True" && "${available:-0}" == "${expected}" ]]; then
      capture_pause "${scaledobject} FALLBACK=True, deployment ${expected}/${expected}"
      if [[ "${restore_grace_seconds}" -gt 0 ]]; then
        echo "Keeping fallback active for ${restore_grace_seconds}s so the original metric window cannot trigger a scale rebound."
        local remaining="${restore_grace_seconds}"
        while [[ "${remaining}" -gt 0 ]]; do
          sleep 15
          remaining=$((remaining - 15))
          echo "$(date '+%H:%M:%S') ${scaledobject} restore grace remaining=$((remaining > 0 ? remaining : 0))s"
        done
      fi
      restore_scaler
      wait_for_deployment kagent "${deployment}" "${expected}" exact
      return 0
    fi
    sleep 5
  done
  echo "${scaledobject} did not converge to fallback replica count ${expected}." >&2
  return 1
}

run_rag() {
  prepare_capacity
  echo "Waiting for RAG baseline 1/1."
  wait_for_deployment api-serving recsys-rag-api 1 exact
  show_status

  kubectl -n api-serving exec -i deployment/recsys-rag-api -c api -- \
    python - "${rag_duration}" "${rag_rps}" \
    >reports/agentic/capture-rag-load.json \
    2>reports/agentic/capture-rag-load.stderr.log <<'PY' &
import concurrent.futures
import json
import sys
import time
import urllib.request

duration, rps = int(sys.argv[1]), int(sys.argv[2])
url = "http://127.0.0.1:8080/v1/rag/retrieve"
body = json.dumps({
    "query": "wireless headphones with good battery life",
    "top_k_items": 5,
    "filters": {},
}).encode()

def invoke():
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()
        return response.status == 200

futures = []
with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
    for _ in range(duration):
        started = time.monotonic()
        futures.extend(executor.submit(invoke) for _ in range(rps))
        time.sleep(max(0, 1 - (time.monotonic() - started)))
outcomes = []
for future in futures:
    try:
        outcomes.append(future.result())
    except Exception:
        outcomes.append(False)
result = {"requests": len(outcomes), "successes": sum(outcomes), "errors": outcomes.count(False)}
print(json.dumps(result, sort_keys=True))
raise SystemExit(result["errors"] / max(1, result["requests"]) >= 0.01)
PY
  load_pid="$!"

  wait_for_scale_with_load api-serving recsys-rag-api 3 \
    reports/agentic/capture-rag-load.stderr.log
  capture_pause "RAG API scaled 1 -> 3 and all three replicas are Available"
  wait_for_load_completion
  cat reports/agentic/capture-rag-load.json
  echo "Waiting for RAG API to return to the 1/1 baseline before restoring capacity."
  wait_for_deployment api-serving recsys-rag-api 1 exact
}

run_mcp() {
  prepare_capacity
  echo "Waiting for MCP baseline 1/1."
  wait_for_deployment kagent recsys-feature-rag-mcp 1 exact

  kubectl -n kagent exec -i deployment/recsys-feature-rag-mcp -c mcp -- \
    python - "${mcp_duration}" "${mcp_rps}" "${chunk_id}" \
    >reports/agentic/capture-mcp-load.json \
    2>reports/agentic/capture-mcp-load.stderr.log <<'PY' &
import asyncio
import json
import os
import statistics
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

duration, rps, chunk_id = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
latencies = []
errors = 0
worker_count = rps * 3
interval = worker_count / rps

async def worker():
    global errors
    headers = {"Authorization": "Bearer " + os.environ["MCP_AUTH_TOKEN"]}
    async with httpx.AsyncClient(headers=headers) as client:
        async with streamable_http_client(
            "http://127.0.0.1:8080/mcp", http_client=client
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                deadline = time.monotonic() + duration
                while time.monotonic() < deadline:
                    started = time.perf_counter()
                    try:
                        result = await session.call_tool(
                            "get_chunk_by_id", {"chunk_id": chunk_id}
                        )
                        errors += int(bool(result.isError))
                    except Exception:
                        errors += 1
                    latencies.append(time.perf_counter() - started)
                    await asyncio.sleep(max(0, interval - (time.perf_counter() - started)))

async def main():
    await asyncio.gather(*(worker() for _ in range(worker_count)))
    p95 = statistics.quantiles(latencies, n=100)[94]
    result = {
        "requests": len(latencies),
        "errors": errors,
        "error_rate": errors / max(1, len(latencies)),
        "p95_seconds": p95,
    }
    print(json.dumps(result, sort_keys=True))
    if result["error_rate"] >= 0.01 or p95 >= 3:
        raise SystemExit(1)

asyncio.run(main())
PY
  load_pid="$!"

  wait_for_scale_with_load kagent recsys-feature-rag-mcp 3 \
    reports/agentic/capture-mcp-load.stderr.log
  capture_pause "MCP scaled 1 -> 3 and all three replicas are Available"
  wait_for_load_completion
  cat reports/agentic/capture-mcp-load.json
  if [[ "${run_fallback}" == "1" ]]; then
    prove_fallback recsys-feature-rag-mcp recsys-feature-rag-mcp 1
  fi
}

run_worker() {
  prepare_capacity
  echo "Waiting for Sandbox WorkerPool baseline 1/1."
  wait_for_deployment kagent recsys-context-sandbox-pool 1 exact

  kubectl -n kagent port-forward service/kagent-controller \
    "${a2a_port}:8083" >reports/agentic/capture-a2a-port-forward.log 2>&1 &
  port_forward_pid="$!"
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${a2a_port}/api/a2a-sandboxes/kagent/recsys-context-agent-sandbox/.well-known/agent-card.json" \
      >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  python3 - "${a2a_port}" "${agent_requests}" "${agent_concurrency}" \
    "${agent_user_id}" "${agent_duration}" \
    >reports/agentic/capture-agent-load.json \
    2>reports/agentic/capture-agent-load.stderr.log <<'PY' &
import concurrent.futures
import json
import sys
import time
import urllib.request
import uuid

port, minimum_requests, concurrency, user_id, duration = (
    sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], int(sys.argv[5])
)
url = f"http://127.0.0.1:{port}/api/a2a-sandboxes/kagent/recsys-context-agent-sandbox/"

def invoke(worker_id, index):
    request_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendMessage",
        "params": {"message": {
            "messageId": request_id,
            "contextId": request_id,
            "role": "ROLE_USER",
            "parts": [{"kind": "text", "text": (
                f"Call get_user_online_features with user_id {user_id} and "
                f"top_k 1. Then reply with the exact user_id. "
                f"Load worker {worker_id}, request {index}."
            )}],
        }},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            body = json.load(response)
        return not bool(body.get("error")) and user_id in json.dumps(body)
    except Exception:
        return False

def load_worker(worker_id, deadline):
    offered = completed = rejected = 0
    while time.monotonic() < deadline or offered < minimum_requests:
        offered += 1
        if invoke(worker_id, offered):
            completed += 1
        else:
            rejected += 1
            # Substrate deliberately applies admission backpressure while all
            # workers are assigned. Keep offering work so newly-created workers
            # become assigned during the next KEDA polling window.
            time.sleep(0.1)
    return offered, completed, rejected

deadline = time.monotonic() + duration
with concurrent.futures.ThreadPoolExecutor(
    max_workers=concurrency
) as executor:
    outcomes = list(executor.map(lambda worker_id: load_worker(worker_id, deadline), range(concurrency)))
result = {
    "offered_requests": sum(item[0] for item in outcomes),
    "grounded_completed_requests": sum(item[1] for item in outcomes),
    "backpressure_rejections": sum(item[2] for item in outcomes),
    "duration_seconds": duration,
}
print(json.dumps(result, sort_keys=True))
if result["grounded_completed_requests"] == 0:
    raise SystemExit(1)
PY
  load_pid="$!"

  wait_for_scale_with_load kagent recsys-context-sandbox-pool 3 \
    reports/agentic/capture-agent-load.stderr.log
  capture_pause "Sandbox WorkerPool scaled 1 -> 3 and generated Deployment is 3/3"
  wait_for_load_completion
  cat reports/agentic/capture-agent-load.json
  echo "Waiting for assigned-worker metric scale-down to restore the 1/1 baseline."
  wait_for_deployment kagent recsys-context-sandbox-pool 1 exact
  if [[ "${run_fallback}" == "1" ]]; then
    prove_fallback recsys-context-sandbox-pool \
      recsys-context-sandbox-pool 1
  fi
  kill "${port_forward_pid}" >/dev/null 2>&1 || true
  port_forward_pid=""
}

case "${mode}" in
  status)
    show_status
    ;;
  rag|mcp|worker|all)
    [[ "${confirm_prod}" == "yes" ]] || {
      echo "Set AGENTIC_CAPTURE_CONFIRM_PROD=yes to run production load." >&2
      exit 2
    }
    case "${mode}" in
      rag) run_rag ;;
      mcp) run_mcp ;;
      worker) run_worker ;;
      all)
        run_rag
        run_mcp
        run_worker
        ;;
    esac
    show_status
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
