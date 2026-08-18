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
    metadata:
      labels: {app.kubernetes.io/name: ${job}, app.kubernetes.io/component: rag-indexing}
      annotations: {sidecar.istio.io/inject: "false"}
    spec:
      restartPolicy: Never
      nodeSelector: {recsys.ai/pool: cpu-services}
      containers:
        - name: feast-apply
          image: ${image}
          command: ["bash", "-c"]
          args:
            - >-
              export FEAST_SQL_REGISTRY_URL="\$(python -m recsys_feature_store_runtime.sql_registry_state url)";
              feast -c apps/data-platform/feature-store/rag_feature_repo apply --skip-source-validation --no-progress
          envFrom:
            - configMapRef: {name: recsys-data-platform-config}
            - secretRef: {name: recsys-data-platform-secret}
          env:
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
    metadata:
      labels: {app.kubernetes.io/name: ${job}, app.kubernetes.io/component: rag-indexing}
      annotations: {sidecar.istio.io/inject: "false"}
    spec:
      restartPolicy: Never
      nodeSelector: {recsys.ai/workload: ml-system}
      tolerations:
        - {key: recsys.ai/workload, operator: Equal, value: ml-system, effect: NoSchedule}
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
    metadata:
      annotations: {sidecar.istio.io/inject: "false"}
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
            - {name: MILVUS_HOST, value: "http://recsys-milvus.recsys-dataflow.svc.cluster.local"}
EOF
  rag_wait_job "${namespace}" "${job}" 300s
}

rag_start_api_port_forward() {
  local port="${RAG_API_VERIFY_PORT:-18089}"
  local namespace="${API_NAMESPACE:-api-serving}"
  local ready_pod=""
  mkdir -p .ci-deploy

  # A Service port-forward can select a Pending pod during a rolling update.
  # Wait for the deployment, then pin the tunnel to a Running pod so promotion
  # cannot fail nondeterministically because another replica is still Pending.
  kubectl -n "${namespace}" rollout status deployment/recsys-rag-api --timeout=300s
  ready_pod="$(kubectl -n "${namespace}" get pods \
    -l app.kubernetes.io/name=recsys-rag-api \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}')"
  if [[ -z "${ready_pod}" ]]; then
    echo "No Running RAG API pod is available for promotion verification" >&2
    return 1
  fi

  kubectl -n "${namespace}" port-forward "pod/${ready_pod}" "${port}:8080" >.ci-deploy/rag-api-port-forward.log 2>&1 &
  RAG_API_FORWARD_PID=$!
  for _ in $(seq 1 30); do
    if curl --fail --silent "http://127.0.0.1:${port}/ready" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  kill "${RAG_API_FORWARD_PID}" 2>/dev/null || true
  return 1
}

rag_stop_api_port_forward() {
  if [[ -n "${RAG_API_FORWARD_PID:-}" ]]; then
    kill "${RAG_API_FORWARD_PID}" 2>/dev/null || true
    unset RAG_API_FORWARD_PID
  fi
}

rag_verify_api_contract() {
  local expected_model="${RAG_EMBEDDING_MODEL:-intfloat/multilingual-e5-small}"
  local expected_revision="${RAG_EMBEDDING_REVISION:-03415a4be176a1620747c692ed433219fabc3def}"
  local expected_dimension="${RAG_EMBEDDING_DIMENSION:-384}"
  local port="${RAG_API_VERIFY_PORT:-18089}"
  local report=".ci-deploy/rag-api-version.json"
  rag_start_api_port_forward
  if ! curl --fail --silent --show-error "http://127.0.0.1:${port}/version" >"${report}"; then
    rag_stop_api_port_forward
    return 1
  fi
  rag_stop_api_port_forward
  # Keep the promotion gate on the base Python runtime already required by the
  # Jenkins controller; production agents do not guarantee an external jq
  # binary. The report contains only public model contract metadata.
  python3 - "${report}" "${expected_model}" "${expected_revision}" "${expected_dimension}" <<'PY'
import json
import sys

report_path, expected_model, expected_revision, expected_dimension = sys.argv[1:]
with open(report_path, encoding="utf-8") as handle:
    payload = json.load(handle)

matched = any(
    contract.get("model") == expected_model
    and contract.get("revision") == expected_revision
    and contract.get("dimension") == int(expected_dimension)
    for contract in payload.get("supported_embedding_contracts", [])
)
if not matched:
    raise SystemExit("RAG API does not support the candidate embedding contract")
PY
}

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
    --maximum-p95-ms "${RAG_MAXIMUM_API_P95_MS:-750}" \
    --concurrency 10; then
    rag_stop_api_port_forward
    rag_rollback_pointer "${image}" "${pipeline_run}"
    return 1
  fi
  rag_stop_api_port_forward
}
