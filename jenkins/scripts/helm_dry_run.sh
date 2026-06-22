#!/usr/bin/env bash
set -euo pipefail

helm lint infra/helm/mlflow-stack
helm template recsys-mlflow infra/helm/mlflow-stack --namespace mlops >/tmp/recsys-mlflow.yaml

helm lint infra/helm/recsys-runtime
helm template recsys-runtime infra/helm/recsys-runtime --namespace kubeflow --set namespace.name=kubeflow >/tmp/recsys-runtime.yaml

helm lint infra/helm/ray-cluster
helm template recsys-ray-cpu infra/helm/ray-cluster --namespace kubeflow >/tmp/recsys-ray-cpu.yaml
