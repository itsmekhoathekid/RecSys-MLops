#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 --image <repository:tag> --run-id <id> [--limit <n>] [--workers <n>] [--namespace <ns>] [--values <file>] [--timeout <duration>] [--force]"
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAMESPACE="recsys-dataflow"
LIMIT="0"
WORKERS="4"
RUN_ID=""
IMAGE=""
VALUES_FILE=""
FORCE="false"
HELM_TIMEOUT="3h"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) IMAGE="${2:?missing image}"; shift 2 ;;
    --run-id) RUN_ID="${2:?missing run id}"; shift 2 ;;
    --limit) LIMIT="${2:?missing limit}"; shift 2 ;;
    --workers) WORKERS="${2:?missing workers}"; shift 2 ;;
    --namespace) NAMESPACE="${2:?missing namespace}"; shift 2 ;;
    --values) VALUES_FILE="${2:?missing values file}"; shift 2 ;;
    --timeout) HELM_TIMEOUT="${2:?missing timeout}"; shift 2 ;;
    --force) FORCE="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$IMAGE" || -z "$RUN_ID" ]]; then
  usage >&2
  exit 2
fi
if [[ ! "$LIMIT" =~ ^[0-9]+$ ]]; then
  echo "--limit must be a non-negative integer" >&2
  exit 2
fi
if [[ ! "$WORKERS" =~ ^[1-9][0-9]*$ ]]; then
  echo "--workers must be a positive integer" >&2
  exit 2
fi
if [[ ! "$HELM_TIMEOUT" =~ ^[1-9][0-9]*[smh]$ ]]; then
  echo "--timeout must be a positive Helm duration such as 45m or 3h" >&2
  exit 2
fi
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "--run-id contains unsupported characters" >&2
  exit 2
fi
if [[ "$IMAGE" != *:* ]]; then
  echo "--image must include an explicit tag" >&2
  exit 2
fi

ENV_FILE="$REPO_ROOT/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi
API_KEY_LINE="$(grep -E '^[[:space:]]*orcarouter_deepseekv4_pro=' "$ENV_FILE" | tail -n 1 || true)"
API_KEY="${API_KEY_LINE#*=}"
API_KEY="${API_KEY%$'\r'}"
if [[ "$API_KEY" == \"*\" && "$API_KEY" == *\" ]]; then
  API_KEY="${API_KEY:1:${#API_KEY}-2}"
elif [[ "$API_KEY" == \'*\' && "$API_KEY" == *\' ]]; then
  API_KEY="${API_KEY:1:${#API_KEY}-2}"
fi
if [[ -z "$API_KEY" ]]; then
  echo "orcarouter_deepseekv4_pro is missing or empty in .env" >&2
  exit 1
fi

SECRET_NAME="rag-item-metadata-orcarouter"
cleanup_secret() {
  kubectl -n "$NAMESPACE" delete secret "$SECRET_NAME" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup_secret EXIT

kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
  --from-literal="ORCAROUTER_API_KEY=$API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
unset API_KEY API_KEY_LINE

IMAGE_REPOSITORY="${IMAGE%:*}"
IMAGE_TAG="${IMAGE##*:}"
RELEASE_SUFFIX="$(date -u +%Y%m%d%H%M%S)"
# Helm release names are limited to 53 characters. Keep room for the fixed
# prefix, separators, and the 14-character UTC timestamp suffix.
SAFE_RUN_ID="$(printf '%s' "$RUN_ID" | tr '[:upper:]_.' '[:lower:]--' | cut -c1-28)"
RELEASE_NAME="rag-items-${SAFE_RUN_ID}-${RELEASE_SUFFIX}"

HELM_ARGS=(
  upgrade --install "$RELEASE_NAME" "$REPO_ROOT/infra/helm/recsys-rag-data"
  --namespace "$NAMESPACE"
  --set-string "namespace.name=$NAMESPACE"
  --set-string "image.repository=$IMAGE_REPOSITORY"
  --set-string "image.tag=$IMAGE_TAG"
  --set-string "job.runId=$RUN_ID"
  --set "job.limit=$LIMIT"
  --set "job.workers=$WORKERS"
  --set "job.force=$FORCE"
  --wait --wait-for-jobs --timeout "$HELM_TIMEOUT"
)
if [[ -n "$VALUES_FILE" ]]; then
  HELM_ARGS+=(--values "$VALUES_FILE")
fi

helm "${HELM_ARGS[@]}"
kubectl -n "$NAMESPACE" logs "job/$RELEASE_NAME" --all-containers=false
echo "RAG item metadata Job completed: $RELEASE_NAME"
