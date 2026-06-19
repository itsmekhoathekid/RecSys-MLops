SHELL := /bin/bash

DATAFLOW_SCRIPTS_DIR := deployments/docker/scripts
DATAFLOW_DAG ?= full_dataflow_local_dag
DATAFLOW_SMOKE_PHASE ?= all
DATAFLOW_LOG_SERVICE ?=
DATAFLOW_INGEST_BUCKET ?= recsys-lake
DATAFLOW_INGEST_PREFIX ?= raw
RECSYS_PIPELINE_IMAGE ?= recsys-mlops-training:local
MINIKUBE_PROFILE ?= recsys-mlops
KFP_VERSION ?= 2.16.1
KUBEFLOW_NAMESPACE ?= kubeflow
MLOPS_NAMESPACE ?= mlops

.PHONY: help
help:
	@echo "RecSys MLOps local dataflow commands"
	@echo ""
	@echo "Docker stack:"
	@echo "  make dataflow-up              Start local dataflow stack"
	@echo "  make dataflow-up-build        Build images and start stack"
	@echo "  make dataflow-build           Build dataflow Docker images"
	@echo "  make dataflow-down            Stop and remove dataflow containers"
	@echo "  make dataflow-down-volumes    Stop stack and remove volumes"
	@echo "  make dataflow-restart         Restart stack"
	@echo "  make dataflow-ps              Show service status"
	@echo "  make dataflow-logs            Tail all logs"
	@echo "  make dataflow-logs DATAFLOW_LOG_SERVICE=airflow-webserver"
	@echo ""
	@echo "Pipeline:"
	@echo "  make dataflow-e2e             Trigger one full E2E DAG run"
	@echo "  make dataflow-ingest-lake     Generate historical data into MinIO lake raw"
	@echo "  make dataflow-realtime-up     Start continuous realtime producer + streaming consumer"
	@echo "  make dataflow-realtime-down   Stop continuous realtime containers"
	@echo "  make dataflow-smoke           Run smoke checks, phase defaults to all"
	@echo "  make dataflow-smoke DATAFLOW_SMOKE_PHASE=services|buckets|connectors|bronze|offline|redis"
	@echo "  make dataflow-trigger         Trigger full_dataflow_local_dag"
	@echo "  make dataflow-test            Run local unit tests"
	@echo ""
	@echo "Kubeflow/MLflow:"
	@echo "  make mlops-local-up           Start local minikube profile"
	@echo "  make mlops-images             Build training and MLflow images"
	@echo "  make mlops-images-minikube    Build images inside minikube Docker daemon"
	@echo "  make mlops-install-kfp        Install standalone Kubeflow Pipelines"
	@echo "  make mlops-install-kuberay    Install KubeRay operator"
	@echo "  make mlops-install-stack      Install MLflow/runtime Helm charts"
	@echo "  make mlops-compile-kfp        Compile the RecSys BST Kubeflow pipeline"
	@echo "  make mlops-helm-template      Render MLflow/runtime Helm charts"
	@echo "  make mlops-port-forward       Port-forward KFP, MLflow, MinIO, Ray dashboards"

.PHONY: mlops-local-up
mlops-local-up:
	@minikube start --profile $(MINIKUBE_PROFILE) --driver=docker --cpus=6 --memory=12288 --disk-size=40g
	@kubectl config use-context $(MINIKUBE_PROFILE)

.PHONY: dataflow-build
dataflow-build:
	@$(DATAFLOW_SCRIPTS_DIR)/dataflow_build.sh

.PHONY: dataflow-up
dataflow-up:
	@$(DATAFLOW_SCRIPTS_DIR)/dataflow_up.sh

.PHONY: dataflow-up-build
dataflow-up-build:
	@$(DATAFLOW_SCRIPTS_DIR)/dataflow_up.sh --build

.PHONY: dataflow-down
dataflow-down:
	@$(DATAFLOW_SCRIPTS_DIR)/dataflow_down.sh

.PHONY: dataflow-down-volumes
dataflow-down-volumes:
	@$(DATAFLOW_SCRIPTS_DIR)/dataflow_down.sh --volumes

.PHONY: dataflow-restart
dataflow-restart: dataflow-down dataflow-up

.PHONY: dataflow-ps
dataflow-ps:
	@$(DATAFLOW_SCRIPTS_DIR)/dataflow_ps.sh

.PHONY: dataflow-logs
dataflow-logs:
	@$(DATAFLOW_SCRIPTS_DIR)/dataflow_logs.sh $(DATAFLOW_LOG_SERVICE)

.PHONY: dataflow-smoke
dataflow-smoke:
	@$(DATAFLOW_SCRIPTS_DIR)/dataflow_smoke.sh $(DATAFLOW_SMOKE_PHASE)

.PHONY: dataflow-trigger
dataflow-trigger:
	@$(DATAFLOW_SCRIPTS_DIR)/dataflow_trigger_dag.sh $(DATAFLOW_DAG)

.PHONY: dataflow-e2e
dataflow-e2e:
	@$(DATAFLOW_SCRIPTS_DIR)/dataflow_run_e2e.sh $(DATAFLOW_DAG) $(DATAFLOW_SMOKE_PHASE)

.PHONY: dataflow-ingest-lake
dataflow-ingest-lake:
	@$(DATAFLOW_SCRIPTS_DIR)/dataflow_ingest_lake.sh $(DATAFLOW_INGEST_BUCKET) $(DATAFLOW_INGEST_PREFIX)

.PHONY: dataflow-realtime-up
dataflow-realtime-up:
	@$(DATAFLOW_SCRIPTS_DIR)/dataflow_realtime_up.sh

.PHONY: dataflow-realtime-down
dataflow-realtime-down:
	@$(DATAFLOW_SCRIPTS_DIR)/dataflow_realtime_down.sh

.PHONY: dataflow-test
dataflow-test:
	@uv run pytest data_generator/tests testing/unit -q

.PHONY: mlops-images
mlops-images:
	@docker build -f deployments/docker/Dockerfile.base-python -t recsys-base-python:local .
	@docker build -f deployments/docker/Dockerfile.training -t recsys-mlops-training:local .
	@docker build -f deployments/docker/Dockerfile.mlflow -t recsys-mlflow:local .

.PHONY: mlops-images-minikube
mlops-images-minikube:
	@eval "$$(minikube -p $(MINIKUBE_PROFILE) docker-env)" && $(MAKE) mlops-images

.PHONY: mlops-install-kfp
mlops-install-kfp:
	@kubectl apply -k "github.com/kubeflow/pipelines/manifests/kustomize/cluster-scoped-resources?ref=$(KFP_VERSION)"
	@kubectl wait --for condition=established --timeout=60s crd/applications.app.k8s.io
	@kubectl apply -k "github.com/kubeflow/pipelines/manifests/kustomize/env/dev?ref=$(KFP_VERSION)"

.PHONY: mlops-install-kuberay
mlops-install-kuberay:
	@helm repo add kuberay https://ray-project.github.io/kuberay-helm/
	@helm repo update
	@helm upgrade --install kuberay-operator kuberay/kuberay-operator --namespace $(KUBEFLOW_NAMESPACE) --create-namespace

.PHONY: mlops-install-stack
mlops-install-stack:
	@helm upgrade --install recsys-mlflow deployments/helm/mlflow-stack --namespace $(MLOPS_NAMESPACE) --create-namespace
	@helm upgrade --install recsys-runtime deployments/helm/recsys-runtime --namespace $(KUBEFLOW_NAMESPACE) --set namespace.name=$(KUBEFLOW_NAMESPACE)

.PHONY: mlops-compile-kfp
mlops-compile-kfp:
	@RECSYS_PIPELINE_IMAGE=$(RECSYS_PIPELINE_IMAGE) python deployments/kubeflow/pipelines/recsys_bst_pipeline.py

.PHONY: mlops-helm-template
mlops-helm-template:
	@helm template recsys-mlflow deployments/helm/mlflow-stack --namespace mlops
	@helm template recsys-runtime deployments/helm/recsys-runtime --namespace $(KUBEFLOW_NAMESPACE) --set namespace.name=$(KUBEFLOW_NAMESPACE)
	@helm template recsys-ray-cpu deployments/helm/ray-cluster --namespace $(KUBEFLOW_NAMESPACE)
	@helm template recsys-ray-gpu deployments/helm/ray-cluster --namespace $(KUBEFLOW_NAMESPACE) -f deployments/helm/ray-cluster/values-gpu.yaml

.PHONY: mlops-port-forward
mlops-port-forward:
	@echo "KFP UI:    kubectl port-forward -n $(KUBEFLOW_NAMESPACE) svc/ml-pipeline-ui 8080:80"
	@echo "MLflow:    kubectl port-forward -n $(MLOPS_NAMESPACE) svc/mlflow 5000:5000"
	@echo "MinIO:     kubectl port-forward -n $(MLOPS_NAMESPACE) svc/minio 9001:9001"
	@echo "Ray UI:    kubectl port-forward -n $(KUBEFLOW_NAMESPACE) svc/recsys-bst-ray-tune-raycluster-*-head-svc 8265:8265"
