#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."

namespace="${SUBSTRATE_BENCHMARK_NAMESPACE:-kagent}"
agent_name="${SUBSTRATE_BENCHMARK_AGENT:-recsys-recommendation-agent-sandbox}"
worker_pool="${SUBSTRATE_BENCHMARK_WORKER_POOL:-recsys-recommendation-sandbox-pool}"
iterations="${SUBSTRATE_BENCHMARK_ITERATIONS:-3}"
local_port="${SUBSTRATE_BENCHMARK_LOCAL_PORT:-18086}"
settle_seconds="${SUBSTRATE_BENCHMARK_SETTLE_SECONDS:-10}"
inter_iteration_seconds="${SUBSTRATE_BENCHMARK_INTER_ITERATION_SECONDS:-10}"
request_timeout="${SUBSTRATE_BENCHMARK_REQUEST_TIMEOUT_SECONDS:-180}"
benchmark_prompt="${SUBSTRATE_BENCHMARK_PROMPT:-This is a runtime readiness check, not a recommendation request. Reply with exactly: warm-ok}"
run_id="${SUBSTRATE_BENCHMARK_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
report_dir="${SUBSTRATE_BENCHMARK_REPORT_DIR:-reports/agentic/substrate-warmup-${run_id}}"
base_url="http://127.0.0.1:${local_port}/api/a2a-sandboxes/${namespace}/${agent_name}"
port_forward_log="${report_dir}/port-forward.log"

if ! [[ "${iterations}" =~ ^[1-9][0-9]*$ ]]; then
  echo "SUBSTRATE_BENCHMARK_ITERATIONS must be a positive integer." >&2
  exit 2
fi
if ! [[ "${settle_seconds}" =~ ^[0-9]+$ ]]; then
  echo "SUBSTRATE_BENCHMARK_SETTLE_SECONDS must be a non-negative integer." >&2
  exit 2
fi
if ! [[ "${inter_iteration_seconds}" =~ ^[0-9]+$ ]]; then
  echo "SUBSTRATE_BENCHMARK_INTER_ITERATION_SECONDS must be a non-negative integer." >&2
  exit 2
fi

for command in kubectl python3; do
  command -v "${command}" >/dev/null 2>&1 || {
    echo "${command} is required." >&2
    exit 2
  }
done

mkdir -p "${report_dir}/responses"

cleanup() {
  if [[ -n "${port_forward_pid:-}" ]]; then
    kill "${port_forward_pid}" >/dev/null 2>&1 || true
    wait "${port_forward_pid}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

kubectl -n "${namespace}" wait --for=condition=Ready \
  "sandboxagent/${agent_name}" --timeout=10m

desired_replicas="$(kubectl -n "${namespace}" get workerpool "${worker_pool}" \
  -o jsonpath='{.spec.replicas}')"
ready_replicas="$(kubectl -n "${namespace}" get workerpool "${worker_pool}" \
  -o jsonpath='{.status.replicas}')"
if [[ "${desired_replicas:-0}" -lt 1 || "${ready_replicas:-0}" -lt 1 ]]; then
  echo "WorkerPool ${worker_pool} is not warm: desired=${desired_replicas:-0}, ready=${ready_replicas:-0}." >&2
  exit 1
fi

kubectl -n "${namespace}" get sandboxagent "${agent_name}" -o json \
  >"${report_dir}/sandboxagent.json"
kubectl -n "${namespace}" get actortemplate "${agent_name}" -o json \
  >"${report_dir}/actortemplate.json"
kubectl -n "${namespace}" get workerpool "${worker_pool}" -o json \
  >"${report_dir}/workerpool-before.json"
kubectl -n "${namespace}" get pods \
  -l "ate.dev/worker-pool=${worker_pool}" -o json \
  >"${report_dir}/worker-pods-before.json"
if kubectl -n "${namespace}" get scaledobject "${worker_pool}" -o json \
  >"${report_dir}/scaledobject.json" 2>"${report_dir}/scaledobject.stderr"; then
  :
else
  rm -f "${report_dir}/scaledobject.json"
fi

run_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kubectl -n "${namespace}" port-forward service/kagent-controller \
  "${local_port}:8083" >"${port_forward_log}" 2>&1 &
port_forward_pid=$!

card_ready=false
for _ in $(seq 1 30); do
  if python3 - "${base_url}/.well-known/agent-card.json" <<'PY' 2>/dev/null
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
  then
    card_ready=true
    break
  fi
  sleep 1
done
if [[ "${card_ready}" != "true" ]]; then
  echo "kagent A2A endpoint did not become ready; see ${port_forward_log}." >&2
  exit 1
fi

python3 - \
  "${base_url}/" \
  "${iterations}" \
  "${request_timeout}" \
  "${inter_iteration_seconds}" \
  "${benchmark_prompt}" \
  "${report_dir}/client-results.json" \
  "${report_dir}/responses" <<'PY'
import json
import pathlib
import sys
import time
import urllib.request
import uuid

(
    url,
    iterations_raw,
    timeout_raw,
    inter_iteration_raw,
    benchmark_prompt,
    output_path,
    response_dir_raw,
) = sys.argv[1:]
iterations = int(iterations_raw)
timeout = int(timeout_raw)
inter_iteration_seconds = int(inter_iteration_raw)
response_dir = pathlib.Path(response_dir_raw)
measurements = []

for index in range(1, iterations + 1):
    request_id = str(uuid.uuid4())
    context_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {
            "message": {
                "messageId": request_id,
                "contextId": context_id,
                "role": "user",
                "parts": [
                    {
                        "kind": "text",
                        "text": benchmark_prompt,
                    }
                ],
            }
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.load(response)
        status_code = response.status
    elapsed_ms = (time.perf_counter() - started) * 1000
    response_path = response_dir / f"iteration-{index:02d}.json"
    response_path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    if body.get("error"):
        raise SystemExit(f"iteration {index} returned A2A error: {body['error']}")
    state = body.get("result", {}).get("status", {}).get("state")
    if state != "completed":
        raise SystemExit(f"iteration {index} did not complete: state={state!r}")
    measurements.append(
        {
            "iteration": index,
            "request_id": request_id,
            "context_id": context_id,
            "http_status": status_code,
            "elapsed_ms": round(elapsed_ms, 3),
        }
    )
    print(f"warm iteration {index}/{iterations}: {elapsed_ms:.1f} ms")
    if index < iterations:
        time.sleep(inter_iteration_seconds)

pathlib.Path(output_path).write_text(
    json.dumps({"measurements": measurements}, indent=2, sort_keys=True) + "\n"
)
PY

sleep "${settle_seconds}"
run_finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

kubectl -n "${namespace}" get workerpool "${worker_pool}" -o json \
  >"${report_dir}/workerpool-after.json"
kubectl -n "${namespace}" get pods \
  -l "ate.dev/worker-pool=${worker_pool}" -o json \
  >"${report_dir}/worker-pods-after.json"
kubectl -n ate-system logs deployment/ate-api-server \
  --since-time="${run_started_at}" >"${report_dir}/ate-api.log"

python3 - \
  "${report_dir}" \
  "${namespace}" \
  "${agent_name}" \
  "${worker_pool}" \
  "${run_started_at}" \
  "${run_finished_at}" <<'PY'
import datetime as dt
import json
import math
import pathlib
import re
import statistics
import sys

report_dir_raw, namespace, agent_name, worker_pool, started_at, finished_at = sys.argv[1:]
report_dir = pathlib.Path(report_dir_raw)


def load(name):
    return json.loads((report_dir / name).read_text())


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def duration_ms(value):
    units = {"ns": 1e-6, "µs": 1e-3, "us": 1e-3, "ms": 1.0, "s": 1000.0}
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ns|µs|us|ms|s)", value)
    if not match:
        raise ValueError(f"unsupported Go duration: {value}")
    return float(match.group(1)) * units[match.group(2)]


def percentile(values, quantile):
    ordered = sorted(values)
    if not ordered:
        return None
    rank = math.ceil(quantile * len(ordered)) - 1
    return round(ordered[max(rank, 0)], 3)


agent = load("sandboxagent.json")
before_pool = load("workerpool-before.json")
after_pool = load("workerpool-after.json")
before_pods = load("worker-pods-before.json")
after_pods = load("worker-pods-after.json")
client = load("client-results.json")

created_at = parse_time(agent["metadata"]["creationTimestamp"])
ready_condition = next(
    condition
    for condition in agent.get("status", {}).get("conditions", [])
    if condition.get("type") == "Ready" and condition.get("status") == "True"
)
golden_ready_at = parse_time(ready_condition["lastTransitionTime"])

pod_startup = []
for pod in before_pods.get("items", []):
    ready = next(
        (
            condition
            for condition in pod.get("status", {}).get("conditions", [])
            if condition.get("type") == "Ready" and condition.get("status") == "True"
        ),
        None,
    )
    if ready:
        latency = (parse_time(ready["lastTransitionTime"]) - parse_time(
            pod["metadata"]["creationTimestamp"]
        )).total_seconds() * 1000
        pod_startup.append(
            {"pod": pod["metadata"]["name"], "ready_ms": round(latency, 3)}
        )

actor_template_by_id = {}
first_resume_by_actor = {}
suspend_by_actor = {}
for raw_line in (report_dir / "ate-api.log").read_text().splitlines():
    start = raw_line.find("{")
    if start < 0:
        continue
    try:
        event = json.loads(raw_line[start:])
    except json.JSONDecodeError:
        continue
    method = event.get("method", "")
    request = event.get("req") or {}
    response = event.get("resp") or {}
    actor = response.get("actor") or {}
    actor_id = request.get("actor_id") or actor.get("actor_id")
    template = actor.get("actor_template_name") or request.get("actor_template_name")
    if actor_id and template:
        actor_template_by_id[actor_id] = template
    if not actor_id or event.get("err") is not None:
        continue
    if method.endswith("/ResumeActor") and actor_id not in first_resume_by_actor:
        first_resume_by_actor[actor_id] = duration_ms(event["elapsed-time"])
    elif method.endswith("/SuspendActor") and actor_id not in suspend_by_actor:
        suspend_by_actor[actor_id] = duration_ms(event["elapsed-time"])

resume = [
    value
    for actor_id, value in first_resume_by_actor.items()
    if actor_template_by_id.get(actor_id) == agent_name
]
suspend = [
    value
    for actor_id, value in suspend_by_actor.items()
    if actor_template_by_id.get(actor_id) == agent_name
]
client_latencies = [item["elapsed_ms"] for item in client["measurements"]]
before_uids = sorted(item["metadata"]["uid"] for item in before_pods.get("items", []))
after_uids = sorted(item["metadata"]["uid"] for item in after_pods.get("items", []))
expected_samples = len(client_latencies)
if len(resume) != expected_samples:
    raise SystemExit(
        f"expected {expected_samples} actor resume samples, found {len(resume)}"
    )
if len(suspend) != expected_samples:
    raise SystemExit(
        f"expected {expected_samples} actor snapshot samples, found {len(suspend)}"
    )

scaledobject_path = report_dir / "scaledobject.json"
scaling = None
if scaledobject_path.exists():
    scaledobject = load("scaledobject.json")
    spec = scaledobject.get("spec", {})
    scaling = {
        "min_replicas": spec.get("minReplicaCount"),
        "max_replicas": spec.get("maxReplicaCount"),
        "fallback_replicas": (spec.get("fallback") or {}).get("replicas"),
    }

summary = {
    "schema_version": 1,
    "run": {
        "started_at": started_at,
        "finished_at": finished_at,
        "namespace": namespace,
        "sandbox_agent": agent_name,
        "worker_pool": worker_pool,
        "iterations": len(client_latencies),
    },
    "warm_up_proof": {
        "sandbox_agent_ready": True,
        "ready_reason": ready_condition.get("reason"),
        "ready_message": ready_condition.get("message"),
        "golden_snapshot_one_time_build_ms": round(
            (golden_ready_at - created_at).total_seconds() * 1000, 3
        ),
        "worker_pool_desired_before": before_pool.get("spec", {}).get("replicas"),
        "worker_pool_ready_before": before_pool.get("status", {}).get("replicas"),
        "worker_pool_desired_after": after_pool.get("spec", {}).get("replicas"),
        "worker_pool_ready_after": after_pool.get("status", {}).get("replicas"),
        "worker_pod_uids_unchanged": before_uids == after_uids,
        "worker_pod_uids_before": before_uids,
        "worker_pod_uids_after": after_uids,
        "autoscaling": scaling,
    },
    "worker_pod_ready_latency": {
        "samples": pod_startup,
        "p50_ms": round(statistics.median([x["ready_ms"] for x in pod_startup]), 3)
        if pod_startup
        else None,
        "p95_ms": percentile([x["ready_ms"] for x in pod_startup], 0.95),
    },
    "actor_resume_latency": {
        "sample_count": len(resume),
        "samples_ms": [round(value, 3) for value in resume],
        "p50_ms": round(statistics.median(resume), 3) if resume else None,
        "p95_ms": percentile(resume, 0.95),
    },
    "actor_snapshot_latency": {
        "sample_count": len(suspend),
        "samples_ms": [round(value, 3) for value in suspend],
        "p50_ms": round(statistics.median(suspend), 3) if suspend else None,
        "p95_ms": percentile(suspend, 0.95),
    },
    "a2a_end_to_end_latency": {
        "note": "Includes actor resume, kagent/A2A overhead, and model inference; snapshot completes after the response.",
        "samples_ms": client_latencies,
        "p50_ms": round(statistics.median(client_latencies), 3),
        "p95_ms": percentile(client_latencies, 0.95),
    },
}

(report_dir / "summary.json").write_text(
    json.dumps(summary, indent=2, sort_keys=True) + "\n"
)

print("\nAgent Substrate warm-up benchmark")
print(f"  report: {report_dir}/summary.json")
print(
    "  golden snapshot one-time build: "
    f"{summary['warm_up_proof']['golden_snapshot_one_time_build_ms']:.0f} ms"
)
print(
    "  actor resume p50/p95: "
    f"{summary['actor_resume_latency']['p50_ms']} / "
    f"{summary['actor_resume_latency']['p95_ms']} ms"
)
print(
    "  actor snapshot p50/p95: "
    f"{summary['actor_snapshot_latency']['p50_ms']} / "
    f"{summary['actor_snapshot_latency']['p95_ms']} ms"
)
print(
    "  A2A end-to-end p50/p95: "
    f"{summary['a2a_end_to_end_latency']['p50_ms']} / "
    f"{summary['a2a_end_to_end_latency']['p95_ms']} ms"
)
print(
    "  worker pod UIDs unchanged: "
    f"{summary['warm_up_proof']['worker_pod_uids_unchanged']}"
)
PY
