#!/usr/bin/env bash

rag_index_promote() {
  local image="$1"
  local source_run="${RAG_SOURCE_RUN_ID:?RAG_SOURCE_RUN_ID is required and must reference a complete canonical manifest}"
  local pipeline_run="${RAG_PIPELINE_RUN_ID:?RAG_PIPELINE_RUN_ID is required}"
  local expected_items="${RAG_EXPECTED_ITEM_COUNT:-160}"
  local cpu_request="${RAG_PROMOTION_CPU_REQUEST:-100m}"
  local smoke_run="${pipeline_run}-smoke"
  local namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
  local job="recsys-rag-index-promotion"
  # A candidate cannot be promoted until the deployed query encoder advertises
  # the exact model revision and dimension used to bake passage embeddings.
  rag_verify_api_contract
  kubectl -n "${namespace}" delete job "${job}" --ignore-not-found --wait=true
  kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata: {name: ${job}, namespace: ${namespace}}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels: {app.kubernetes.io/name: ${job}, app.kubernetes.io/component: rag-indexing}
      annotations: {sidecar.istio.io/inject: "false"}
    spec:
      restartPolicy: Never
      # Serving owns the constrained CPU pool during promotion. The transient
      # encoder job uses capacity released on the ML node after the API moves,
      # then exits and returns those resources to online workloads.
      nodeSelector: {recsys.ai/workload: ml-system}
      tolerations:
        - {key: recsys.ai/workload, operator: Equal, value: ml-system, effect: NoSchedule}
      securityContext: {runAsNonRoot: true, runAsUser: 10001, runAsGroup: 10001}
      containers:
        - name: index
          image: ${image}
          command: ["bash", "-c"]
          args:
            - >-
              set -euo pipefail;
              python -m rag_data.cli chunk-items --config configs/data-platform/rag/pipeline.yaml --source-run-id '${source_run}' --run-id '${smoke_run}' --item-limit 3 --force;
              python -m rag_data.cli embed-chunks --config configs/data-platform/rag/pipeline.yaml --run-id '${smoke_run}' --checkpoint-every 32 --force;
              python -m rag_data.cli publish-index --config configs/data-platform/rag/pipeline.yaml --run-id '${smoke_run}' --mode reconcile;
              python -m rag_data.cli validate-index --config configs/data-platform/rag/pipeline.yaml --run-id '${smoke_run}' --expected-item-count 3;
              python -m rag_data.cli chunk-items --config configs/data-platform/rag/pipeline.yaml --source-run-id '${source_run}' --run-id '${pipeline_run}';
              python -m rag_data.cli embed-chunks --config configs/data-platform/rag/pipeline.yaml --run-id '${pipeline_run}' --checkpoint-every 32;
              python -m rag_data.cli publish-index --config configs/data-platform/rag/pipeline.yaml --run-id '${pipeline_run}' --mode reconcile;
              python -m rag_data.cli validate-index --config configs/data-platform/rag/pipeline.yaml --run-id '${pipeline_run}' --expected-item-count '${expected_items}' --promote
          envFrom:
            - configMapRef: {name: recsys-data-platform-config}
            - secretRef: {name: recsys-data-platform-secret}
          env:
            - {name: MILVUS_HOST, value: "http://recsys-milvus.recsys-dataflow.svc.cluster.local"}
            - {name: RAG_FEAST_REPO, value: "apps/data-platform/feature-store/rag_feature_repo"}
          resources:
            # The ONNX encoder can burst to two cores while a small configurable
            # request keeps this resumable one-shot Job schedulable when the
            # production node pools are at their quota ceiling.
            requests: {cpu: "${cpu_request}", memory: 2Gi}
            limits: {cpu: "2", memory: 4Gi}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}}
          volumeMounts: [{name: tmp, mountPath: /tmp}]
      volumes: [{name: tmp, emptyDir: {}}]
EOF
  rag_wait_job "${namespace}" "${job}" 3600s

  local port="${RAG_API_VERIFY_PORT:-18089}"
  rag_start_api_port_forward
  # This post-promotion gate exercises the deployed query encoder, Feast,
  # Milvus and grouping logic together. Failure restores the previous ETag-
  # guarded pointer before the release is reported unsuccessful.
  if ! python3 scripts/rag/verify_retrieval.py \
    --base-url "http://127.0.0.1:${port}" \
    --golden configs/data-platform/rag/golden_queries.json \
    --report .ci-deploy/rag-retrieval-verification.json \
    --minimum-recall "${RAG_MINIMUM_RECALL_AT_10:-0.90}" \
    --indexed-item-count "${expected_items}" \
    --maximum-p95-ms "${RAG_MAXIMUM_API_P95_MS:-750}" \
    --concurrency 10; then
    rag_stop_api_port_forward
    rag_rollback_pointer "${image}" "${pipeline_run}"
    return 1
  fi
  rag_stop_api_port_forward
}
