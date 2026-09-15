#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
source jenkins/scripts/deploy/agentic/rotation.sh

namespace="${KAGENT_NAMESPACE:-kagent}"
service="${1:?usage: $0 <featureRag|recommendation> <revision> [duration-seconds]}"
wanted_revision="${2:?revision is required}"
duration="${3:-${MCP_AUTH_PROBE_DURATION_SECONDS:-900}}"
interval="${MCP_AUTH_PROBE_INTERVAL_SECONDS:-2}"

[[ "${duration}" =~ ^[1-9][0-9]*$ ]] || {
  printf 'probe duration must be a positive integer number of seconds\n' >&2
  exit 2
}
[[ "${interval}" =~ ^([1-9][0-9]*(\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)$ ]] || {
  printf 'probe interval must be greater than zero\n' >&2
  exit 2
}

record="$({ mcp_auth_list_deployed "${service}" || true; } \
  | awk -F '\t' -v wanted="${wanted_revision}" '$1 == wanted { print; exit }')"
[[ -n "${record}" ]] || {
  printf 'revision %s is not deploy:true for %s\n' \
    "${wanted_revision}" "${service}" >&2
  exit 2
}
IFS=$'\t' read -r revision secret_name workload_name vault_version <<<"${record}"

kubectl -n "${namespace}" rollout status "deployment/${workload_name}" \
  --timeout="${MCP_AUTH_PROBE_READY_TIMEOUT:-600s}"
kubectl -n "${namespace}" exec "deployment/${workload_name}" -c mcp -- env \
  "MCP_ROTATION_TARGET=http://${workload_name}.${namespace}.svc.cluster.local:8080/mcp" \
  "MCP_ROTATION_DURATION=${duration}" \
  "MCP_ROTATION_INTERVAL=${interval}" \
  python -c '
import asyncio
import json
import os
import time

import httpx


async def main() -> None:
    deadline = time.monotonic() + int(os.environ["MCP_ROTATION_DURATION"])
    interval = float(os.environ["MCP_ROTATION_INTERVAL"])
    attempts = failures = 0
    headers = {
        "Authorization": "Bearer " + os.environ["MCP_AUTH_TOKEN"],
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": "continuous-auth-probe",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mcp-auth-rotation", "version": "1"},
        },
    }
    async with httpx.AsyncClient(timeout=30) as client:
        while time.monotonic() < deadline:
            attempts += 1
            try:
                response = await client.post(
                    os.environ["MCP_ROTATION_TARGET"],
                    content=json.dumps(payload),
                    headers=headers,
                )
                failures += int(response.status_code != 200)
            except Exception:
                failures += 1
            await asyncio.sleep(interval)
    print(json.dumps({"attempts": attempts, "auth_failures": failures}, sort_keys=True))
    if attempts == 0 or failures:
        raise SystemExit(1)


asyncio.run(main())
'

printf 'Continuous MCP auth probe passed service=%s revision=%s workload=%s vault=%s secret=%s\n' \
  "${service}" "${revision}" "${workload_name}" "${vault_version}" "${secret_name}"
