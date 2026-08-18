#!/usr/bin/env bash

datahub_wait_job() {
  local namespace="$1" job="$2" timeout="${3:-600s}"
  if ! kubectl wait -n "${namespace}" --for=condition=complete "job/${job}" --timeout="${timeout}" >/dev/null; then
    kubectl logs -n "${namespace}" "job/${job}" --all-containers=true || true
    return 1
  fi
  kubectl logs -n "${namespace}" "job/${job}" --all-containers=true
}

datahub_catalog_action() {
  local action="$1" image="$2"
  local namespace="${DATAHUB_NAMESPACE:-datahub}"
  local gms_url="${DATAHUB_GMS_URL:-http://datahub-datahub-gms.datahub.svc.cluster.local:8080}"
  local job="recsys-datahub-catalog-${action}-${BUILD_NUMBER:-manual}"
  local args='["--strict"]'
  if [[ "${action}" == "verify" ]]; then
    args='["--verify-only", "--strict"]'
  fi
  kubectl delete job -n "${namespace}" "${job}" --ignore-not-found >/dev/null
  kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job}
  namespace: ${namespace}
  labels: {app.kubernetes.io/name: recsys-datahub-catalog}
spec:
  backoffLimit: 1
  ttlSecondsAfterFinished: 1800
  template:
    metadata:
      labels: {app.kubernetes.io/name: recsys-datahub-catalog}
      annotations: {sidecar.istio.io/inject: "false"}
    spec:
      restartPolicy: Never
      containers:
        - name: catalog-sync
          image: ${image}
          command: ["python", "-m", "metadata.sync_datahub_catalog"]
          args: ${args}
          env:
            - name: DATAHUB_GMS_URL
              value: ${gms_url}
            - name: DATAHUB_TOKEN
              valueFrom:
                secretKeyRef:
                  name: ${DATAHUB_TOKEN_SECRET:-recsys-datahub-token}
                  key: ${DATAHUB_TOKEN_SECRET_KEY:-token}
                  optional: true
EOF
  datahub_wait_job "${namespace}" "${job}" 900s
}

datahub_catalog_sync() {
  datahub_catalog_action sync "$1"
}

datahub_catalog_verify() {
  datahub_catalog_action verify "$1"
}

datahub_catalog_cutover() {
  local mode="$1" manifest="$2" image="$3"
  local namespace="${DATAHUB_NAMESPACE:-datahub}"
  local gms_url="${DATAHUB_GMS_URL:-http://datahub-datahub-gms.datahub.svc.cluster.local:8080}"
  local suffix="${BUILD_NUMBER:-manual}"
  local job="recsys-datahub-cutover-${mode}-${suffix}"
  local configmap="recsys-datahub-cutover-${suffix}"
  mkdir -p "$(dirname "${manifest}")"
  kubectl delete job -n "${namespace}" "${job}" --ignore-not-found >/dev/null
  if [[ "${mode}" == "apply" ]]; then
    [[ -s "${manifest}" ]] || { echo "Cutover manifest is missing: ${manifest}" >&2; return 2; }
    kubectl create configmap -n "${namespace}" "${configmap}" \
      --from-file=manifest.json="${manifest}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  fi
  local args='["--manifest", "/tmp/cutover/manifest.json"]'
  local volume_yaml=""
  if [[ "${mode}" == "apply" ]]; then
    args='["--manifest", "/manifest/manifest.json", "--apply", "--confirm-cutover"]'
    volume_yaml="          volumeMounts:
            - {name: manifest, mountPath: /manifest, readOnly: true}"
  fi
  kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata: {name: ${job}, namespace: ${namespace}}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 1800
  template:
    metadata: {annotations: {sidecar.istio.io/inject: "false"}}
    spec:
      restartPolicy: Never
      containers:
        - name: cutover
          image: ${image}
          command: ["python", "/opt/recsys/ops/migrations/datahub-dataset-lineage-cutover/cutover.py"]
          args: ${args}
          env:
            - {name: PYTHONWARNINGS, value: "ignore"}
            - {name: DATAHUB_GMS_URL, value: "${gms_url}"}
            - name: DATAHUB_TOKEN
              valueFrom:
                secretKeyRef:
                  name: ${DATAHUB_TOKEN_SECRET:-recsys-datahub-token}
                  key: ${DATAHUB_TOKEN_SECRET_KEY:-token}
                  optional: true
${volume_yaml}
$(if [[ "${mode}" == "apply" ]]; then printf '      volumes:\n        - name: manifest\n          configMap: {name: %s}\n' "${configmap}"; fi)
EOF
  if [[ "${mode}" == "plan" ]]; then
    datahub_wait_job "${namespace}" "${job}" 900s | tee "${manifest}"
    python3 -c 'import json, sys; data=json.load(open(sys.argv[1])); assert data.get("dry_run") is True; assert isinstance(data.get("records"), list)' "${manifest}"
  else
    datahub_wait_job "${namespace}" "${job}" 900s
    datahub_catalog_verify "${image}"
  fi
}
