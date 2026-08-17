#!/usr/bin/env bash

test_rag_index() {
  kubectl -n "${namespace_data}" get statefulset -l app.kubernetes.io/name=milvus
  kubectl -n "${namespace_data}" get pvc -l app.kubernetes.io/name=milvus
  kubectl -n "${namespace_data}" get job recsys-rag-feature-registry
  kubectl -n "${namespace_data}" get job recsys-rag-index-promotion
  if kubectl -n "${namespace_data}" logs job/recsys-rag-index-promotion --all-containers=true \
    | grep -Ei 'ORCAROUTER_API_KEY|AWS_SECRET_ACCESS_KEY|MILVUS_PASSWORD'; then
    recsys_error "RAG promotion log contains a secret variable name"
    return 1
  fi
}

test_rag_api() {
  kubectl -n "${namespace_api}" rollout status deployment/recsys-rag-api --timeout="${timeout}"
  local port="${RAG_API_VERIFY_PORT:-18089}"
  kubectl -n "${namespace_api}" port-forward service/recsys-rag-api "${port}:80" >.ci-deploy/rag-api-verify.log 2>&1 &
  local pid=$!
  sleep 3
  curl --fail --silent "http://127.0.0.1:${port}/healthz" >/dev/null
  curl --fail --silent "http://127.0.0.1:${port}/ready" >/dev/null
  curl --fail --silent "http://127.0.0.1:${port}/version" \
    | grep -q supported_embedding_contracts
  curl --fail --silent \
    -H 'content-type: application/json' \
    -d '{"query":"tai nghe chống ồn văn phòng","top_k_items":3,"filters":{"in_stock":true}}' \
    "http://127.0.0.1:${port}/v1/rag/retrieve" >.ci-deploy/rag-retrieval-verify.json
  kill "${pid}" 2>/dev/null || true
}
