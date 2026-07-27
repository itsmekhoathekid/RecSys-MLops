#!/usr/bin/env bash

deploy_mlflow() {
  helm_atomic_upgrade recsys-mlflow infra/helm/mlflow-stack \
    "${namespace_mlops}" "${timeout}" \
    --reuse-values \
    --set "nodeSelector.recsys\\.ai/pool=ml-system" \
    --set "tolerations[0].key=recsys.ai/workload" \
    --set "tolerations[0].operator=Equal" \
    --set "tolerations[0].value=ml-system" \
    --set "tolerations[0].effect=NoSchedule" \
    --set "minio.resources.requests.cpu=100m" \
    --set "minio.resources.requests.memory=512Mi" \
    --set "postgres.resources.requests.cpu=100m" \
    --set "postgres.resources.requests.memory=256Mi" \
    --set "mlflow.resources.requests.cpu=100m" \
    --set "mlflow.resources.requests.memory=512Mi" \
    --set "mlflow.image=$(image recsys-mlflow)" \
    --set "mlflow.imagePullPolicy=Always"
  verify_and_wait_workload deployment mlflow "${namespace_mlops}" "$(image recsys-mlflow)"
  wait_rollout_if_exists deployment minio "${namespace_mlops}"
  wait_rollout_if_exists deployment postgres "${namespace_mlops}"
}

deploy_training_refs() {
  local training_image spark_image drift_retrain_image kfp_upload_state=""
  training_image="$(image recsys-mlops-training)"
  spark_image="$(image recsys-mlops-spark)"
  drift_retrain_image="$(image recsys-drift-retrain)"
  if [[ "${TX_ACTIVE}" == "1" ]]; then
    kfp_upload_state="${TX_DIR}/kfp-upload.json"
    tx_register_external kfp-version "${kfp_upload_state}"
  fi
  KFP_UPLOAD_RESULT_PATH="${kfp_upload_state}" \
    KFP_ENDPOINT="$(kfp_endpoint_for_upload)" \
    RECSYS_PIPELINE_IMAGE="${training_image}" \
    RECSYS_RAY_IMAGE="${training_image}" \
    RECSYS_SPARK_IMAGE="${spark_image}" \
    bash jenkins/scripts/kubeflow_pipeline_cicd.sh
  deploy_mlflow
  deploy_data_platform --set "images.driftRetrain=${drift_retrain_image}"
  verify_data_platform_config_image "DRIFT_RETRAIN_IMAGE" "${drift_retrain_image}"
  wait_rollout_if_exists deployment airflow-scheduler "${namespace_data}"
}
