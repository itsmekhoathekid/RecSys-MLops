#!/usr/bin/env bash

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
