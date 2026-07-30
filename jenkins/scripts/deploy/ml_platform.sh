#!/usr/bin/env bash

deploy_mlflow() {
  helm_atomic_upgrade recsys-mlflow infra/helm/mlflow-stack \
    "${namespace_mlops}" "${timeout}" \
    --reuse-values \
    --set "secret.create=false" \
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
