#!/usr/bin/env bash
set -euo pipefail

PROFILE="${MINIKUBE_PROFILE:-recsys-mlops}"

echo "Stopping Minikube profile ${PROFILE}..."
minikube -p "${PROFILE}" stop

echo
echo "Cluster stopped. Current status:"
minikube -p "${PROFILE}" status || true

echo
echo "Docker node container:"
if docker inspect "${PROFILE}" >/dev/null 2>&1; then
  docker inspect "${PROFILE}" --format 'memory_bytes={{.HostConfig.Memory}} memory_swap_bytes={{.HostConfig.MemorySwap}} state={{.State.Status}}'
else
  echo "Docker container ${PROFILE} not found"
fi
