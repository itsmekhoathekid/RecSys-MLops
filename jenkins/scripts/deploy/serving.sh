#!/usr/bin/env bash

deploy_api_unlocked() {
  local helm_args=(
    upgrade --install recsys-serving infra/helm/recsys-serving
    --namespace "${namespace_kserve}" --create-namespace --reuse-values
    --atomic --cleanup-on-fail --wait --wait-for-jobs
    --history-max "${HELM_HISTORY_MAX:-10}" --timeout "${timeout}"
    --set "api.namespace.name=${namespace_api}"
    --set "api.image=$(image recsys-api-serving)"
    --set "api.imagePullPolicy=Always"
    --set "featureApi.image=$(image recsys-api-serving)"
    --set "featureApi.imagePullPolicy=Always"
    --set "kserve.secret.create=false"
    --set "shadow.enabled=false"
    --set "shadow.samplePercent=100"
    --set "shadow.timeoutMs=1000"
    --set "shadow.queueSize=100"
    --set "shadow.maxConcurrency=4"
  )
  [[ -n "${API_ROLLOUT_MAX_SURGE:-}" ]] && helm_args+=(--set "api.rollout.maxSurge=${API_ROLLOUT_MAX_SURGE}")
  [[ -n "${API_ROLLOUT_MAX_UNAVAILABLE:-}" ]] && helm_args+=(--set "api.rollout.maxUnavailable=${API_ROLLOUT_MAX_UNAVAILABLE}")
  helm "${helm_args[@]}"
  verify_and_wait_workload deployment recsys-online-feature-api "${namespace_api}" "$(image recsys-api-serving)"
  verify_and_wait_workload deployment recsys-api-serving "${namespace_api}" "$(image recsys-api-serving)"
}

deploy_api() {
  with_file_lock "/tmp/recsys-serving-helm.lock" deploy_api_unlocked
}

deploy_kserve_unlocked() {
  load_secret_env_if_unset "${namespace_kubeflow}" "${MLOPS_RUNTIME_SECRET_NAME:-recsys-mlops-runtime}" \
    AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION MINIO_ENDPOINT \
    MINIO_ROOT_USER MINIO_ROOT_PASSWORD MLFLOW_S3_ENDPOINT_URL MODEL_STORE_ENDPOINT \
    MODEL_STORE_BUCKET MODEL_STORE_PREFIX
  configure_local_model_store_endpoint
  RECSYS_MODEL_CD_ATOMIC="${RECSYS_MODEL_CD_ATOMIC:-0}" \
    uv run --no-project --with boto3 python -m jenkins.python.model_cd.cli \
    --manifest-uri "${promotion_manifest_uri}" \
    --output-dir .model-cd \
    --timeout "${timeout}"
}

deploy_kserve_model_cd_unlocked() {
  load_secret_env_if_unset "${namespace_kubeflow}" "${MLOPS_RUNTIME_SECRET_NAME:-recsys-mlops-runtime}" \
    AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION MINIO_ENDPOINT \
    MINIO_ROOT_USER MINIO_ROOT_PASSWORD MLFLOW_S3_ENDPOINT_URL MODEL_STORE_ENDPOINT \
    MODEL_STORE_BUCKET MODEL_STORE_PREFIX
  configure_local_model_store_endpoint
  local model_cd_args=(
    --manifest-uri "${promotion_manifest_uri}"
    --stage "${MODEL_CD_STAGE:-deploy}"
    --candidate-weight-percent "${AB_CANDIDATE_WEIGHT_PERCENT:-10}"
    --output-dir .model-cd
    --timeout "${timeout}"
  )
  [[ -n "${CONTROL_MANIFEST_URI:-}" ]] && model_cd_args+=(--control-manifest-uri "${CONTROL_MANIFEST_URI}")
  [[ -n "${CANDIDATE_MANIFEST_URI:-}" ]] && model_cd_args+=(--candidate-manifest-uri "${CANDIDATE_MANIFEST_URI}")
  [[ -n "${AB_EXPERIMENT_ID:-}" ]] && model_cd_args+=(--experiment-id "${AB_EXPERIMENT_ID}")
  [[ -n "${PROMETHEUS_URL:-}" ]] && model_cd_args+=(--prometheus-url "${PROMETHEUS_URL}")
  [[ -n "${AB_GATE_WINDOW:-}" ]] && model_cd_args+=(--gate-window "${AB_GATE_WINDOW}")
  [[ -n "${AB_MIN_SAMPLES:-}" ]] && model_cd_args+=(--min-samples "${AB_MIN_SAMPLES}")
  [[ "${MODEL_CD_APPLY:-1}" == "1" ]] && model_cd_args+=(--apply)
  RECSYS_MODEL_CD_ATOMIC="${RECSYS_MODEL_CD_ATOMIC:-1}" \
    uv run --no-project --with boto3 python -m jenkins.python.model_cd.cli "${model_cd_args[@]}"
}

deploy_kserve() {
  with_file_lock "/tmp/recsys-serving-helm.lock" deploy_kserve_unlocked
}

deploy_kserve_model_cd() {
  with_file_lock "/tmp/recsys-serving-helm.lock" deploy_kserve_model_cd_unlocked
}
