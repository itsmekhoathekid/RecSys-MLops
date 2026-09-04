#!/usr/bin/env bash

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
