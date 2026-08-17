#!/usr/bin/env bash

# Deployment state machine for the RAG registry and blue/green index. Helm rolls
# back application resources atomically; this script changes the active pointer
# only after candidate gates and restores it if the external API smoke fails.

rag_wait_job() {
  local namespace="$1" job="$2" timeout="${3:-1800s}"
  if ! kubectl -n "${namespace}" wait --for=condition=complete "job/${job}" --timeout="${timeout}"; then
    kubectl -n "${namespace}" logs "job/${job}" --all-containers=true || true
    return 1
  fi
  kubectl -n "${namespace}" logs "job/${job}" --all-containers=true
}

rag_feature_registry_apply() {
  local image="$1"
  local namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
  local job="recsys-rag-feature-registry"
  kubectl -n "${namespace}" delete job "${job}" --ignore-not-found --wait=true
  kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata: {name: ${job}, namespace: ${namespace}}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    metadata: {labels: {app.kubernetes.io/name: ${job}, app.kubernetes.io/component: rag-indexing}}
    spec:
      restartPolicy: Never
      nodeSelector: {recsys.ai/pool: cpu-services}
      containers:
        - name: feast-apply
          image: ${image}
          command: ["bash", "-c"]
          args: ["feast -c apps/data-platform/feature-store/rag_feature_repo apply --skip-source-validation --no-progress"]
          envFrom:
            - configMapRef: {name: recsys-data-platform-config}
            - secretRef: {name: recsys-data-platform-secret}
          env:
            - {name: RUNTIME_LINEAGE_ENABLED, value: "false"}
            - {name: MILVUS_HOST, value: "http://recsys-milvus.recsys-dataflow.svc.cluster.local"}
EOF
  rag_wait_job "${namespace}" "${job}" 600s
}

rag_milvus_credentials_bootstrap() {
  local image="$1"
  local namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
  local job="recsys-milvus-credentials"
  kubectl -n "${namespace}" delete job "${job}" --ignore-not-found --wait=true
  kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata: {name: ${job}, namespace: ${namespace}}
spec:
  backoffLimit: 3
  ttlSecondsAfterFinished: 3600
  template:
    metadata: {labels: {app.kubernetes.io/name: ${job}, app.kubernetes.io/component: rag-indexing}}
    spec:
      restartPolicy: Never
      nodeSelector: {recsys.ai/pool: cpu-services}
      containers:
        - name: rotate-root
          image: ${image}
          command: ["/opt/venv/bin/python", "-c"]
          args:
            - |
              import os
              import time
              from pymilvus import MilvusClient

              uri = "http://recsys-milvus.recsys-dataflow.svc.cluster.local:19530"
              new_password = os.environ["MILVUS_PASSWORD"]
              for attempt in range(30):
                  try:
                      client = MilvusClient(uri=uri, token=f"root:{new_password}")
                      client.list_collections()
                      client.close()
                      raise SystemExit(0)
                  except Exception:
                      pass
                  try:
                      client = MilvusClient(uri=uri, token="root:Milvus")
                      client.update_password("root", "Milvus", new_password, reset_connection=True)
                      client.list_collections()
                      client.close()
                      raise SystemExit(0)
                  except Exception:
                      if attempt == 29:
                          raise RuntimeError("Milvus credential bootstrap failed")
                      time.sleep(5)
          envFrom:
            - secretRef: {name: recsys-data-platform-secret}
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {cpu: "1", memory: 1Gi}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}}
EOF
  # The job first verifies the Vault-managed password, making retries safe. It
  # uses Milvus's public initial credential only once, before any API/index job
  # is admitted by the deployment dependency graph and NetworkPolicy.
  rag_wait_job "${namespace}" "${job}" 300s
}

rag_rollback_pointer() {
  local image="$1" pipeline_run="$2"
  local namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
  local job="recsys-rag-index-rollback"
  kubectl -n "${namespace}" delete job "${job}" --ignore-not-found --wait=true
  kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata: {name: ${job}, namespace: ${namespace}}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: rollback
          image: ${image}
          command: ["python", "-m", "rag_data.cli"]
          args: ["rollback-index", "--config", "configs/data-platform/rag/pipeline.yaml", "--run-id", "${pipeline_run}"]
          envFrom:
            - configMapRef: {name: recsys-data-platform-config}
            - secretRef: {name: recsys-data-platform-secret}
          env:
            - {name: RUNTIME_LINEAGE_STRICT, value: "true"}
            - {name: MILVUS_HOST, value: "http://recsys-milvus.recsys-dataflow.svc.cluster.local"}
EOF
  rag_wait_job "${namespace}" "${job}" 300s
}

rag_verify_api_contract() {
  local expected_model="${RAG_EMBEDDING_MODEL:-intfloat/multilingual-e5-small}"
  local expected_revision="${RAG_EMBEDDING_REVISION:-03415a4be176a1620747c692ed433219fabc3def}"
  local expected_dimension="${RAG_EMBEDDING_DIMENSION:-384}"
  local port="${RAG_API_VERIFY_PORT:-18089}"
  local report=".ci-deploy/rag-api-version.json"
  mkdir -p .ci-deploy
  kubectl -n "${API_NAMESPACE:-api-serving}" port-forward service/recsys-rag-api "${port}:80" >.ci-deploy/rag-api-port-forward.log 2>&1 &
  local forward_pid=$!
  sleep 3
  if ! curl --fail --silent --show-error "http://127.0.0.1:${port}/version" >"${report}"; then
    kill "${forward_pid}" 2>/dev/null || true
    return 1
  fi
  kill "${forward_pid}" 2>/dev/null || true
  jq -e \
    --arg model "${expected_model}" \
    --arg revision "${expected_revision}" \
    --argjson dimension "${expected_dimension}" \
    '.supported_embedding_contracts | any(.model == $model and .revision == $revision and .dimension == $dimension)' \
    "${report}" >/dev/null
}

rag_index_promote() {
  local image="$1"
  local source_run="${RAG_SOURCE_RUN_ID:?RAG_SOURCE_RUN_ID is required and must reference a complete canonical manifest}"
  local pipeline_run="${RAG_PIPELINE_RUN_ID:?RAG_PIPELINE_RUN_ID is required}"
  local expected_items="${RAG_EXPECTED_ITEM_COUNT:-160}"
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
    metadata: {labels: {app.kubernetes.io/name: ${job}, app.kubernetes.io/component: rag-indexing}}
    spec:
      restartPolicy: Never
      nodeSelector: {recsys.ai/pool: cpu-services}
      securityContext: {runAsNonRoot: true, runAsUser: 10001, runAsGroup: 10001}
      containers:
        - name: index
          image: ${image}
          command: ["bash", "-c"]
          args:
            - >-
              set -euo pipefail;
              python -m rag_data.cli chunk-items --config configs/data-platform/rag/pipeline.yaml --source-run-id '${source_run}' --run-id '${smoke_run}' --item-limit 3 --force;
              python -m rag_data.cli embed-chunks --config configs/data-platform/rag/pipeline.yaml --run-id '${smoke_run}' --force;
              python -m rag_data.cli publish-index --config configs/data-platform/rag/pipeline.yaml --run-id '${smoke_run}' --mode reconcile;
              python -m rag_data.cli validate-index --config configs/data-platform/rag/pipeline.yaml --run-id '${smoke_run}' --expected-item-count 3;
              python -m rag_data.cli chunk-items --config configs/data-platform/rag/pipeline.yaml --source-run-id '${source_run}' --run-id '${pipeline_run}';
              python -m rag_data.cli embed-chunks --config configs/data-platform/rag/pipeline.yaml --run-id '${pipeline_run}';
              python -m rag_data.cli publish-index --config configs/data-platform/rag/pipeline.yaml --run-id '${pipeline_run}' --mode reconcile;
              python -m rag_data.cli validate-index --config configs/data-platform/rag/pipeline.yaml --run-id '${pipeline_run}' --expected-item-count '${expected_items}' --promote
          envFrom:
            - configMapRef: {name: recsys-data-platform-config}
            - secretRef: {name: recsys-data-platform-secret}
          env:
            - {name: RUNTIME_LINEAGE_STRICT, value: "true"}
            - {name: MILVUS_HOST, value: "http://recsys-milvus.recsys-dataflow.svc.cluster.local"}
            - {name: RAG_FEAST_REPO, value: "apps/data-platform/feature-store/rag_feature_repo"}
          resources:
            requests: {cpu: "1", memory: 2Gi}
            limits: {cpu: "2", memory: 4Gi}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}}
          volumeMounts: [{name: tmp, mountPath: /tmp}]
      volumes: [{name: tmp, emptyDir: {}}]
EOF
  rag_wait_job "${namespace}" "${job}" 3600s

  local port="${RAG_API_VERIFY_PORT:-18089}"
  mkdir -p .ci-deploy
  kubectl -n "${API_NAMESPACE:-api-serving}" port-forward service/recsys-rag-api "${port}:80" >.ci-deploy/rag-api-port-forward.log 2>&1 &
  local forward_pid=$!
  sleep 3
  # This post-promotion gate exercises the deployed query encoder, Feast,
  # Milvus and grouping logic together. Failure restores the previous ETag-
  # guarded pointer before the release is reported unsuccessful.
  if ! python3 scripts/rag/verify_retrieval.py \
    --base-url "http://127.0.0.1:${port}" \
    --golden configs/data-platform/rag/golden_queries.json \
    --report .ci-deploy/rag-retrieval-verification.json \
    --minimum-recall "${RAG_MINIMUM_RECALL_AT_10:-0.90}" \
    --maximum-p95-ms "${RAG_MAXIMUM_API_P95_MS:-750}" \
    --concurrency 10; then
    kill "${forward_pid}" 2>/dev/null || true
    rag_rollback_pointer "${image}" "${pipeline_run}"
    return 1
  fi
  kill "${forward_pid}" 2>/dev/null || true
}
