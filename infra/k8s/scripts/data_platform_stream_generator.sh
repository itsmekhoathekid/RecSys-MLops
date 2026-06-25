#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-status}"
NAMESPACE="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
PRODUCER="${DATA_PLATFORM_REALTIME_PRODUCER:-realtime-event-producer}"
REPLICAS="${DATA_PLATFORM_REALTIME_PRODUCER_REPLICAS:-1}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

case "${ACTION}" in
  start)
    kubectl rollout status "deploy/realtime-flink-consumer" -n "${NAMESPACE}" --timeout=180s
    kubectl scale "deploy/${PRODUCER}" -n "${NAMESPACE}" --replicas="${REPLICAS}"
    kubectl rollout status "deploy/${PRODUCER}" -n "${NAMESPACE}" --timeout=180s
    "${ROOT_DIR}/infra/k8s/scripts/data_platform_verify_feature_stores.sh"
    ;;
  stop)
    kubectl scale "deploy/${PRODUCER}" -n "${NAMESPACE}" --replicas=0
    for _ in {1..90}; do
      pod_count="$(kubectl get pods -n "${NAMESPACE}" -l "app=${PRODUCER}" --no-headers 2>/dev/null | wc -l | tr -d ' ')"
      if [[ "${pod_count}" == "0" ]]; then
        echo "Realtime stream generator stopped: ${PRODUCER}"
        exit 0
      fi
      sleep 2
    done
    kubectl get pods -n "${NAMESPACE}" -l "app=${PRODUCER}"
    echo "Timed out waiting for realtime stream generator pods to stop" >&2
    exit 1
    ;;
  status)
    kubectl get "deploy/${PRODUCER}" -n "${NAMESPACE}"
    kubectl get pods -n "${NAMESPACE}" -l "app=${PRODUCER}"
    ;;
  *)
    echo "Usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
