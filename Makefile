SHELL := /bin/bash

DATAFLOW_SCRIPTS_DIR := deployments/docker/scripts
DATAFLOW_DAG ?= full_dataflow_local_dag
DATAFLOW_SMOKE_PHASE ?= all
DATAFLOW_LOG_SERVICE ?=
DATAFLOW_INGEST_BUCKET ?= recsys-lake
DATAFLOW_INGEST_PREFIX ?= raw

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
