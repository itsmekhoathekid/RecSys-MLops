SHELL := /bin/bash

DATAFLOW_SCRIPTS_DIR := infra/docker/scripts
DATAFLOW_DAG ?= full_dataflow_local_dag
DATAFLOW_SMOKE_PHASE ?= all
DATAFLOW_LOG_SERVICE ?=
DATAFLOW_INGEST_BUCKET ?= recsys-lake
DATAFLOW_INGEST_PREFIX ?= raw
RECSYS_PIPELINE_IMAGE ?= recsys-mlops-training:local
MINIKUBE_PROFILE ?= recsys-mlops
MINIKUBE_CPUS ?= 8
MINIKUBE_MEMORY_MB ?= 16384
MINIKUBE_DISK_SIZE ?= 40g
DATA_PLATFORM_NAMESPACE ?= recsys-dataflow
DATA_PLATFORM_REALTIME_PRODUCER ?= realtime-event-producer
DATA_PLATFORM_REALTIME_PRODUCER_REPLICAS ?= 1
DATAHUB_NAMESPACE ?= datahub
DATAHUB_FRONTEND_PORT ?= 9002
DATAHUB_GMS_PORT ?= 8088
DATAHUB_GMS_URL ?= http://127.0.0.1:$(DATAHUB_GMS_PORT)
KFP_VERSION ?= 2.16.1
KUBEFLOW_NAMESPACE ?= kubeflow
MLOPS_NAMESPACE ?= experiment-tracking

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
	@echo "K8s Data Platform:"
	@echo "  make data-platform-images-minikube Build data platform images inside minikube"
	@echo "  make data-platform-template        Render recsys-data-platform Helm chart"
	@echo "  make data-platform-install         Install recsys-data-platform Helm chart"
	@echo "  make data-platform-trigger         Trigger k8s_data_platform_dag"
	@echo "  make data-platform-e2e             Install, wait, trigger, and print run status"
	@echo "  make data-platform-run-status      Print Airflow DAG run status"
	@echo "  make data-platform-verify-e2e      Verify MinIO, warehouse, Redis, dbt/offline outputs"
	@echo "  make data-platform-stream-generator-start  Start realtime data generator"
	@echo "  make data-platform-stream-generator-stop   Stop realtime data generator"
	@echo "  make data-platform-stream-generator-status Show realtime data generator status"
	@echo "  make data-platform-port-forward    Show port-forward commands"
	@echo ""
	@echo "DataHub Governance:"
	@echo "  make datahub-install               Install local DataHub and prerequisites"
	@echo "  make datahub-status                Show DataHub pods, jobs, and Helm releases"
	@echo "  make datahub-port-forward          Show DataHub port-forward commands"
	@echo "  make datahub-ingest-governance     Ingest DP1/DP2/DP3 lineage, validation, and contract metadata"
	@echo ""
	@echo "Kubeflow/MLflow:"
	@echo "  make mlops-local-up           Start local minikube profile"
	@echo "  make mlops-cluster-up         Start minikube and wait for the full RecSys MLOps service stack"
	@echo "  make mlops-cluster-down       Stop the full minikube cluster and all services"
	@echo "  make mlops-cluster-status     Show cluster memory and full service status"
	@echo "  make mlops-images             Build training and MLflow images"
	@echo "  make mlops-images-minikube    Build images inside minikube Docker daemon"
	@echo "  make mlops-install-kfp        Install standalone Kubeflow Pipelines"
	@echo "  make mlops-install-kuberay    Install KubeRay operator"
	@echo "  make mlops-install-stack      Install MLflow/runtime Helm charts"
	@echo "  make mlops-install-serving    Install KServe/Triton and API serving chart"
	@echo "  make mlops-compile-kfp        Compile the RecSys BST Kubeflow pipeline"
	@echo "  make mlops-helm-template      Render MLflow/runtime Helm charts"
	@echo "  make mlops-port-forward       Port-forward KFP, MLflow, MinIO, Ray dashboards"
	@echo ""
	@echo "Observability:"
	@echo "  make observability-template      Render observability Helm chart"
	@echo "  make observability-install       Install Prometheus/Grafana/Loki/Tempo stack"
	@echo "  make observability-port-forward  Show Grafana/Prometheus/Loki/Tempo forwards"
	@echo "  make observability-demo-traffic  Generate API traffic for dashboards"
	@echo "  make observability-smoke         Check observability pods and services"

.PHONY: mlops-local-up
mlops-local-up:
	@$(MAKE) mlops-cluster-up

.PHONY: mlops-cluster-up
mlops-cluster-up:
	@MINIKUBE_PROFILE=$(MINIKUBE_PROFILE) MINIKUBE_CPUS=$(MINIKUBE_CPUS) MINIKUBE_MEMORY_MB=$(MINIKUBE_MEMORY_MB) MINIKUBE_DISK_SIZE=$(MINIKUBE_DISK_SIZE) infra/k8s/scripts/mlops_cluster_up.sh

.PHONY: mlops-cluster-down
mlops-cluster-down:
	@MINIKUBE_PROFILE=$(MINIKUBE_PROFILE) infra/k8s/scripts/mlops_cluster_down.sh

.PHONY: mlops-cluster-status
mlops-cluster-status:
	@MINIKUBE_PROFILE=$(MINIKUBE_PROFILE) infra/k8s/scripts/mlops_cluster_status.sh

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
	@PYTHONPATH=apps/data-platform/data-generator/src uv run pytest tests/unit/data_generator -q
	@PYTHONPATH=apps/data-platform/src:apps/data-platform/data-generator/src uv run pytest tests/unit/data_platform tests/contract -q

.PHONY: data-platform-images-minikube
data-platform-images-minikube:
	@eval "$$(minikube -p $(MINIKUBE_PROFILE) docker-env)" && docker build -f infra/docker/Dockerfile.base-python -t recsys-base-python:local .
	@eval "$$(minikube -p $(MINIKUBE_PROFILE) docker-env)" && docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:local -f apps/data-platform/Dockerfile.dataflow-cli -t recsys-dataflow-cli:local .
	@eval "$$(minikube -p $(MINIKUBE_PROFILE) docker-env)" && docker build -f apps/data-platform/Dockerfile.spark -t recsys-spark:local .
	@eval "$$(minikube -p $(MINIKUBE_PROFILE) docker-env)" && docker build -f apps/data-platform/Dockerfile.flink -t recsys-flink:local .
	@eval "$$(minikube -p $(MINIKUBE_PROFILE) docker-env)" && docker build -f infra/docker/Dockerfile.kafka-connect -t recsys-kafka-connect:local .
	@eval "$$(minikube -p $(MINIKUBE_PROFILE) docker-env)" && docker build -f infra/docker/Dockerfile.airflow -t recsys-airflow:local .

.PHONY: data-platform-template
data-platform-template:
	@helm template recsys-data-platform infra/helm/recsys-data-platform --namespace recsys-dataflow

.PHONY: data-platform-install
data-platform-install:
	@helm upgrade --install recsys-data-platform infra/helm/recsys-data-platform --namespace recsys-dataflow --create-namespace

.PHONY: data-platform-trigger
data-platform-trigger:
	@kubectl exec -n recsys-dataflow deploy/airflow-webserver -- airflow dags unpause k8s_data_platform_dag
	@kubectl exec -n recsys-dataflow deploy/airflow-webserver -- airflow dags trigger k8s_data_platform_dag

.PHONY: data-platform-e2e
data-platform-e2e: data-platform-install
	@kubectl wait --for=condition=ready pod -l app=data-platform-minio -n recsys-dataflow --timeout=240s
	@kubectl wait --for=condition=ready pod -l app=kafka -n recsys-dataflow --timeout=240s
	@kubectl wait --for=condition=ready pod -l app=kafka-connect -n recsys-dataflow --timeout=300s
	@kubectl wait --for=condition=ready pod -l app=warehouse-postgres -n recsys-dataflow --timeout=180s
	@kubectl wait --for=condition=ready pod -l app=redis -n recsys-dataflow --timeout=180s
	@kubectl wait --for=condition=ready pod -l app=airflow-webserver -n recsys-dataflow --timeout=240s
	@$(MAKE) data-platform-trigger
	@$(MAKE) data-platform-run-status

.PHONY: data-platform-run-status
data-platform-run-status:
	@kubectl exec -n recsys-dataflow deploy/airflow-webserver -- airflow dags list-runs -d k8s_data_platform_dag

.PHONY: data-platform-verify-e2e
data-platform-verify-e2e:
	@kubectl run recsys-data-platform-verify -n recsys-dataflow --rm -i --restart=Never --image=recsys-dataflow-cli:local --image-pull-policy=IfNotPresent \
		--env=PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys/apps/data-platform/feature-store/src:/opt/recsys \
		--env=MINIO_ENDPOINT=http://data-platform-minio:9000 \
		--env=MINIO_ROOT_USER=minio \
		--env=MINIO_ROOT_PASSWORD=minio123 \
		--env=AWS_ACCESS_KEY_ID=minio \
		--env=AWS_SECRET_ACCESS_KEY=minio123 \
		--env=AWS_DEFAULT_REGION=us-east-1 \
		--env=LAKE_BUCKET=recsys-lake \
		--env=FEATURE_STORE_BUCKET=recsys-feature-store \
		--env=WAREHOUSE_POSTGRES_HOST=warehouse-postgres \
		--env=WAREHOUSE_POSTGRES_PORT=5432 \
		--env=WAREHOUSE_POSTGRES_DB=recsys_warehouse \
		--env=WAREHOUSE_POSTGRES_USER=recsys \
		--env=WAREHOUSE_POSTGRES_PASSWORD=recsys \
		--env=REDIS_HOST=redis \
		--env=REDIS_PORT=6379 \
		-- python -m local.verify_k8s_data_platform_e2e

.PHONY: data-platform-stream-generator-start
data-platform-stream-generator-start:
	@kubectl scale deploy/$(DATA_PLATFORM_REALTIME_PRODUCER) -n $(DATA_PLATFORM_NAMESPACE) --replicas=$(DATA_PLATFORM_REALTIME_PRODUCER_REPLICAS)
	@kubectl rollout status deploy/$(DATA_PLATFORM_REALTIME_PRODUCER) -n $(DATA_PLATFORM_NAMESPACE) --timeout=180s

.PHONY: data-platform-stream-generator-stop
data-platform-stream-generator-stop:
	@kubectl scale deploy/$(DATA_PLATFORM_REALTIME_PRODUCER) -n $(DATA_PLATFORM_NAMESPACE) --replicas=0

.PHONY: data-platform-stream-generator-status
data-platform-stream-generator-status:
	@kubectl get deploy/$(DATA_PLATFORM_REALTIME_PRODUCER) -n $(DATA_PLATFORM_NAMESPACE)
	@kubectl get pods -n $(DATA_PLATFORM_NAMESPACE) -l app=$(DATA_PLATFORM_REALTIME_PRODUCER)

.PHONY: data-platform-smoke
data-platform-smoke:
	@kubectl get pods -n recsys-dataflow
	@kubectl exec -n recsys-dataflow deploy/airflow-webserver -- airflow dags list | rg k8s_data_platform_dag

.PHONY: data-platform-port-forward
data-platform-port-forward:
	@echo "Airflow: kubectl port-forward -n recsys-dataflow svc/airflow-webserver 8080:8080"
	@echo "Flink:   kubectl port-forward -n recsys-dataflow svc/flink-jobmanager 8082:8081"
	@echo "Data Platform MinIO: kubectl port-forward -n recsys-dataflow svc/data-platform-minio 9002:9001"
	@echo "Redis:   kubectl port-forward -n recsys-dataflow svc/redis 6379:6379"
	@echo "Source Postgres:    kubectl port-forward -n recsys-dataflow svc/source-postgres 5432:5432"
	@echo "Warehouse Postgres: kubectl port-forward -n recsys-dataflow svc/warehouse-postgres 5433:5432"

.PHONY: datahub-install
datahub-install:
	@helm repo add datahub https://helm.datahubproject.io/ || true
	@helm repo update datahub
	@kubectl create namespace $(DATAHUB_NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	@kubectl create secret generic mysql-secrets -n $(DATAHUB_NAMESPACE) --from-literal=mysql-root-password=datahub --from-literal=mysql-replication-password=datahub --from-literal=mysql-password=datahub --from-literal=mysql-cdc-password=datahub --dry-run=client -o yaml | kubectl apply -f -
	@kubectl create secret generic datahub-encryption-secrets -n $(DATAHUB_NAMESPACE) --from-literal=encryption_key_secret=datahub-encryption-key-local --dry-run=client -o yaml | kubectl apply -f -
	@helm upgrade --install prerequisites datahub/datahub-prerequisites -n $(DATAHUB_NAMESPACE) -f infra/helm/datahub-local/prerequisites-values.yaml --timeout 12m
	@kubectl apply -f infra/helm/datahub-local/kafka-alias.yaml
	@helm upgrade --install datahub datahub/datahub -n $(DATAHUB_NAMESPACE) -f infra/helm/datahub-local/datahub-values.yaml --timeout 12m
	@kubectl rollout status deploy/datahub-datahub-gms -n $(DATAHUB_NAMESPACE) --timeout=240s
	@kubectl rollout status deploy/datahub-datahub-frontend -n $(DATAHUB_NAMESPACE) --timeout=240s

.PHONY: datahub-status
datahub-status:
	@helm status prerequisites -n $(DATAHUB_NAMESPACE)
	@helm status datahub -n $(DATAHUB_NAMESPACE)
	@kubectl get pods,jobs,svc -n $(DATAHUB_NAMESPACE) -o wide

.PHONY: datahub-port-forward
datahub-port-forward:
	@echo "DataHub UI:  kubectl port-forward -n $(DATAHUB_NAMESPACE) svc/datahub-datahub-frontend $(DATAHUB_FRONTEND_PORT):9002"
	@echo "DataHub GMS: kubectl port-forward -n $(DATAHUB_NAMESPACE) svc/datahub-datahub-gms $(DATAHUB_GMS_PORT):8080"

.PHONY: datahub-ingest-governance
datahub-ingest-governance:
	@set -euo pipefail; \
	kubectl port-forward -n $(DATAHUB_NAMESPACE) svc/datahub-datahub-gms $(DATAHUB_GMS_PORT):8080 >/tmp/recsys-datahub-gms-port-forward.log 2>&1 & \
	pf_pid=$$!; \
	trap 'kill $$pf_pid >/dev/null 2>&1 || true' EXIT; \
	for _ in $$(seq 1 30); do \
	if curl -fsS $(DATAHUB_GMS_URL)/health >/dev/null 2>&1; then break; fi; \
		sleep 1; \
	done; \
	curl -fsS $(DATAHUB_GMS_URL)/health >/dev/null; \
	PYTHONPATH=apps/data-platform/src uv run python -m metadata.ingest_datahub_governance --gms-url $(DATAHUB_GMS_URL)

.PHONY: mlops-images
mlops-images:
	@docker build -f infra/docker/Dockerfile.base-python -t recsys-base-python:local .
	@docker build -f apps/ml-system/Dockerfile.training -t recsys-mlops-training:local .
	@docker build -f apps/api-serving/Dockerfile -t recsys-api-serving:local .
	@docker build -f infra/docker/Dockerfile.mlflow -t recsys-mlflow:local .

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
	@helm upgrade --install recsys-mlflow infra/helm/mlflow-stack --namespace $(MLOPS_NAMESPACE) --create-namespace
	@helm upgrade --install recsys-runtime infra/helm/recsys-runtime --namespace $(KUBEFLOW_NAMESPACE) --set namespace.name=$(KUBEFLOW_NAMESPACE)

.PHONY: mlops-install-serving
mlops-install-serving:
	@helm upgrade --install recsys-serving infra/helm/recsys-serving --namespace kserve-triton-inference --create-namespace

.PHONY: mlops-compile-kfp
mlops-compile-kfp:
	@PYTHONPATH=apps/ml-system/src:apps/data-platform/src RECSYS_PIPELINE_IMAGE=$(RECSYS_PIPELINE_IMAGE) uv run python apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py

.PHONY: mlops-helm-template
mlops-helm-template:
	@helm template recsys-mlflow infra/helm/mlflow-stack --namespace $(MLOPS_NAMESPACE)
	@helm template recsys-runtime infra/helm/recsys-runtime --namespace $(KUBEFLOW_NAMESPACE) --set namespace.name=$(KUBEFLOW_NAMESPACE)
	@helm template recsys-ray-cpu infra/helm/ray-cluster --namespace $(KUBEFLOW_NAMESPACE)
	@helm template recsys-ray-gpu infra/helm/ray-cluster --namespace $(KUBEFLOW_NAMESPACE) -f infra/helm/ray-cluster/values-gpu.yaml
	@helm template recsys-serving infra/helm/recsys-serving --namespace kserve-triton-inference

.PHONY: mlops-port-forward
mlops-port-forward:
	@echo "KFP UI:    kubectl port-forward -n $(KUBEFLOW_NAMESPACE) svc/ml-pipeline-ui 8080:80"
	@echo "MLflow:    kubectl port-forward -n $(MLOPS_NAMESPACE) svc/mlflow 5000:5000"
	@echo "MinIO:     kubectl port-forward -n $(MLOPS_NAMESPACE) svc/minio 9001:9001"
	@echo "Ray UI:    kubectl port-forward -n $(KUBEFLOW_NAMESPACE) svc/recsys-bst-ray-tune-raycluster-*-head-svc 8265:8265"
	@echo "FastAPI:   kubectl port-forward -n api-serving svc/recsys-api-serving 8088:80"

.PHONY: observability-template
observability-template:
	@helm template recsys-observability infra/helm/recsys-observability --namespace observability

.PHONY: observability-install
observability-install:
	@helm upgrade --install recsys-observability infra/helm/recsys-observability --namespace observability --create-namespace

.PHONY: observability-port-forward
observability-port-forward:
	@echo "Grafana:    kubectl port-forward -n observability svc/recsys-grafana 3000:3000"
	@echo "Prometheus: kubectl port-forward -n observability svc/recsys-prometheus 9090:9090"
	@echo "Loki:       kubectl port-forward -n observability svc/recsys-loki 3100:3100"
	@echo "Tempo:      kubectl port-forward -n observability svc/recsys-tempo 3200:3200"
	@echo "PushGateway:kubectl port-forward -n observability svc/recsys-pushgateway 9091:9091"

.PHONY: observability-smoke
observability-smoke:
	@kubectl get pods,svc -n observability
	@kubectl get deploy -n api-serving recsys-api-serving
	@kubectl get svc -n recsys-dataflow monitoring-sql-exporter

.PHONY: observability-demo-traffic
observability-demo-traffic:
	@for i in $$(seq 1 25); do \
		curl -fsS -X POST http://127.0.0.1:8088/recommendations \
			-H 'Content-Type: application/json' \
			-d '{"user_id":1,"candidate_item_ids":[1,2,3,4,5],"top_k":3}' >/dev/null || true; \
	done
