SHELL := /bin/bash
UV_CACHE_DIR ?= .uv-cache
export UV_CACHE_DIR

GCP_POWER_SCRIPT := ops/gcp/services_power.sh
GCP_VERIFY_SCRIPT := ops/validation/verify_gcp_stack.sh
GCP_FULL_VERIFY_SCRIPT := ops/validation/verify_gcp_full_stack.sh
GCP_TRAIN_SCRIPT := ops/gcp/train_model.sh
LLM_SMOKE_SCRIPT := ops/validation/llm_inference_smoke.sh
LLM_BENCHMARK_SCRIPT := ops/validation/llm_inference_benchmark.sh
KFP_PACKAGE := pipelines/kubeflow/compiled/bst_training_pipeline.yaml

.PHONY: help
help:
	@echo "RecSys MLOps production workflows"
	@echo ""
	@echo "Validation:"
	@echo "  make validate                 Validate image catalog and CI configuration"
	@echo "  make test                     Run the Python test suite"
	@echo "  make helm-validate            Lint and render every production Helm chart"
	@echo "  make terraform-validate       Format-check and validate Terraform when initialized"
	@echo "  make full-cicd-preflight      Run the complete local preflight for Jenkins CI/CD"
	@echo "  make verify-gcp               Run static GCP/Helm verification"
	@echo "  make gcp-full-check           Run static/preflight/live/all full-stack checklist"
	@echo ""
	@echo "Artifacts and operations:"
	@echo "  make compile-kfp              Compile the BST Kubeflow pipeline package"
	@echo "  make gcp-services-down        Hibernate GCP services while preserving PVC data"
	@echo "  make gcp-services-up          Restore GCP services and run smoke checks"
	@echo "  make gcp-services-status      Show GCP service and node-pool status"
	@echo "  make serving-autoscale-load-test  Run production serving load validation"
	@echo "  make llm-inference-smoke          Validate Qwen vLLM CPU, llm-d, and agentgateway"
	@echo "  make llm-inference-benchmark      Benchmark the deployed gateway endpoint"
	@echo "  make test-agentic                 Run MCP unit and cross-chart contract tests"
	@echo "  make helm-agentic                 Lint/render the MCP and kagent charts"
	@echo "  make agentic-preflight            Validate agentic platform prerequisites"
	@echo "  make agentic-smoke                Run MCP and regular/sandbox A2A smoke tests"
	@echo "  make agent-substrate-warmup-benchmark Benchmark warm actor restore/snapshot latency"
	@echo "  make substrate-gke-compatibility Check 0.0.11 certificate and metric prerequisites"
	@echo "  make agentic-autoscale-test       Prove context assigned-worker scale 1 -> 3 -> 1/fallback"
	@echo "  make agentic-registry-smoke       Verify published Registry versions"
	@echo "  make test-recommendation-agentic  Run recommendation MCP/Agent tests"
	@echo "  make helm-recommendation-agentic  Lint/render recommendation charts"
	@echo "  make recommendation-agentic-smoke Validate deployed recommendation runtime"
	@echo "  make recommendation-agentic-autoscale Prove recommendation assigned-worker scale/fallback"
	@echo "  make recommendation-agentic-registry Verify Registry Git SHA"
	@echo "  make test-coordinator-agentic     Run coordinator Helm/contract/E2E tests"
	@echo "  make helm-coordinator-agentic     Lint/render the coordinator chart"
	@echo "  make coordinator-agentic-smoke   Validate coordinator A2A and MCP routing"
	@echo "  make coordinator-agentic-autoscale Prove coordinator assigned-worker scaling 1 -> 3 -> 1"
	@echo "  make coordinator-agentic-registry Verify coordinator registry dependencies"
	@echo "  make jenkins-full             Trigger the full production Jenkins CI/CD job"
	@echo "  make gcp-train-model          Run drift DAG, trigger retraining, and wait for KFP"

.PHONY: validate
validate:
	@python3 jenkins/python/image_catalog.py validate
	@python3 jenkins/python/configuration.py validate

.PHONY: test
test:
	@uv run pytest

.PHONY: helm-validate
helm-validate:
	@set -euo pipefail; \
	for chart_file in infra/helm/*/Chart.yaml; do \
	  chart_dir="$$(dirname "$${chart_file}")"; \
	  if [[ "$${chart_dir}" == "infra/helm/recsys-rag-data" ]]; then \
	    helm lint "$${chart_dir}" -f "$${chart_dir}/values-gcp.yaml" --set job.runId=validation; \
	    helm template validation "$${chart_dir}" -f "$${chart_dir}/values-gcp.yaml" --set job.runId=validation >/dev/null; \
	  elif [[ -f "$${chart_dir}/values-gcp.yaml" ]]; then \
	    helm lint "$${chart_dir}" -f "$${chart_dir}/values-gcp.yaml"; \
	    helm template validation "$${chart_dir}" -f "$${chart_dir}/values-gcp.yaml" >/dev/null; \
	  elif [[ "$${chart_dir}" == "infra/helm/recsys-ci" ]]; then \
	    helm lint "$${chart_dir}" -f "$${chart_dir}/values-gke.yaml"; \
	    helm template validation "$${chart_dir}" -f "$${chart_dir}/values-gke.yaml" >/dev/null; \
	  else \
	    helm lint "$${chart_dir}"; \
	    helm template validation "$${chart_dir}" >/dev/null; \
	  fi; \
	done

.PHONY: terraform-validate
terraform-validate:
	@terraform -chdir=infra/terraform/gcp fmt -check -recursive
	@if [[ -d infra/terraform/gcp/.terraform ]]; then \
	  terraform -chdir=infra/terraform/gcp validate; \
	else \
	  echo "Terraform is not initialized; skipped validate."; \
	fi

.PHONY: full-cicd-preflight
full-cicd-preflight: validate helm-validate terraform-validate
	@find jenkins/scripts ops -type f -name '*.sh' -print0 | xargs -0 bash -n
	@uv run pytest tests/unit/jenkins tests/contract -q

.PHONY: compile-kfp
compile-kfp:
	@mkdir -p "$$(dirname "$(KFP_PACKAGE)")"
	@RECSYS_PIPELINE_IMAGE=registry.example.invalid/recsys/recsys-mlops-training:required \
	  RECSYS_RAY_IMAGE=registry.example.invalid/recsys/recsys-mlops-training:required \
	  RECSYS_SPARK_IMAGE=registry.example.invalid/recsys/recsys-spark:required \
	  uv run python apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py \
	  --package-path "$(KFP_PACKAGE)"
	@uv run python apps/ml-system/src/kubeflow/validate_pipeline_package.py \
	  --package-path "$(KFP_PACKAGE)" \
	  --required-image registry.example.invalid/recsys/recsys-mlops-training:required \
	  --required-image registry.example.invalid/recsys/recsys-spark:required \
	  --forbidden-token "recsys-mlops-spark" \
	  --forbidden-token "recsys-analytics-spark"

.PHONY: verify-gcp
verify-gcp:
	@bash "$(GCP_VERIFY_SCRIPT)" static

.PHONY: gcp-full-check
gcp-full-check:
	@if [ -f "$(CURDIR)/.env" ]; then . "$(CURDIR)/.env"; fi; \
		GCP_CHECK_BASIC_AUTH_USER="$${GATEWAY_AUTH_USER:-$${GATEWAY_USER:-}}" \
		GCP_CHECK_BASIC_AUTH_PASSWORD="$${GATEWAY_AUTH_PASSWORD:-$${GATEWAY_PASSWORD:-}}" \
		bash "$(GCP_FULL_VERIFY_SCRIPT)" "$${GCP_CHECK_MODE:-all}"

.PHONY: gcp-train-model
gcp-train-model:
	@bash "$(GCP_TRAIN_SCRIPT)"

.PHONY: gcp-services-down
gcp-services-down:
	@bash "$(GCP_POWER_SCRIPT)" down

.PHONY: gcp-services-up
gcp-services-up:
	@bash "$(GCP_POWER_SCRIPT)" up

.PHONY: gcp-services-status
gcp-services-status:
	@bash "$(GCP_POWER_SCRIPT)" status

.PHONY: serving-autoscale-load-test
serving-autoscale-load-test:
	@bash ops/validation/serving_autoscale_load_test.sh

.PHONY: llm-inference-smoke
llm-inference-smoke:
	@bash "$(LLM_SMOKE_SCRIPT)"

.PHONY: llm-inference-benchmark
llm-inference-benchmark:
	@bash "$(LLM_BENCHMARK_SCRIPT)"

.PHONY: jenkins-full
jenkins-full:
	@bash ops/gcp/trigger_full_jenkins.sh

.PHONY: test-agentic
test-agentic:
	@uv run --project apps/agentic/recsys-feature-rag-mcp pytest \
	  tests/unit/agentic/feature_rag_mcp \
	  tests/integration/feature_rag_mcp \
	  tests/contract/test_agentic_context_contracts.py -q

.PHONY: helm-agentic
helm-agentic:
	@helm lint infra/helm/recsys-feature-rag-mcp
	@helm template validation infra/helm/recsys-feature-rag-mcp >/dev/null
	@helm lint infra/helm/recsys-kagent-agent
	@helm template validation infra/helm/recsys-kagent-agent >/dev/null

.PHONY: agentic-preflight
agentic-preflight:
	@AGENTIC_SMOKE_CHUNK_ID="$${AGENTIC_SMOKE_CHUNK_ID:-preflight-not-used}" \
	  bash -c 'source jenkins/scripts/lib/common.sh; source jenkins/scripts/deploy/agentic.sh; timeout=10m; agentic_preflight true'

.PHONY: agentic-smoke
agentic-smoke:
	@bash ops/validation/agentic_context_smoke.sh

.PHONY: agent-substrate-warmup-benchmark
agent-substrate-warmup-benchmark:
	@bash ops/validation/agent_substrate_warmup_benchmark.sh

.PHONY: substrate-gke-compatibility
substrate-gke-compatibility:
	@bash ops/validation/substrate_gke_compatibility.sh

.PHONY: agentic-autoscale-test
agentic-autoscale-test:
	@bash ops/validation/agentic_context_autoscale.sh

.PHONY: agentic-registry-smoke
agentic-registry-smoke:
	@bash ops/validation/agentic_context_registry_smoke.sh

.PHONY: test-recommendation-agentic
test-recommendation-agentic:
	@uv run --project apps/agentic/recsys-recommendation-mcp pytest \
	  tests/unit/agentic/recommendation_mcp \
	  tests/integration/recommendation_agentic \
	  tests/contract/test_recommendation_agentic_contracts.py \
	  tests/e2e/recommendation_agentic -q

.PHONY: helm-recommendation-agentic
helm-recommendation-agentic:
	@helm lint infra/helm/recsys-recommendation-mcp
	@helm template validation infra/helm/recsys-recommendation-mcp >/dev/null
	@helm lint infra/helm/recsys-recommendation-agent
	@helm template validation infra/helm/recsys-recommendation-agent >/dev/null

.PHONY: recommendation-agentic-preflight
recommendation-agentic-preflight:
	@bash -c 'source jenkins/scripts/lib/common.sh; source jenkins/scripts/deploy/agentic.sh; timeout=10m; recommendation_agentic_preflight true'

.PHONY: recommendation-agentic-smoke
recommendation-agentic-smoke:
	@bash ops/validation/recommendation_agentic_smoke.sh

.PHONY: recommendation-agentic-autoscale
recommendation-agentic-autoscale:
	@bash ops/validation/recommendation_agentic_autoscale.sh all

.PHONY: recommendation-agentic-registry
recommendation-agentic-registry:
	@bash ops/validation/recommendation_agentic_registry_smoke.sh

.PHONY: recommendation-agentic-latency
recommendation-agentic-latency:
	@bash ops/validation/recommendation_agentic_latency.sh

.PHONY: test-coordinator-agentic
test-coordinator-agentic:
	@uv run --project apps/agentic/recsys-feature-rag-mcp pytest \
	  tests/contract/test_coordinator_agentic_contracts.py \
	  tests/e2e/coordinator_agentic -q

.PHONY: helm-coordinator-agentic
helm-coordinator-agentic:
	@helm lint infra/helm/recsys-coordinator-agent \
	  -f infra/helm/recsys-coordinator-agent/values-gcp.yaml
	@helm template validation infra/helm/recsys-coordinator-agent \
	  -f infra/helm/recsys-coordinator-agent/values-gcp.yaml >/dev/null

.PHONY: coordinator-agentic-preflight
coordinator-agentic-preflight:
	@bash -c 'source jenkins/scripts/lib/common.sh; source jenkins/scripts/deploy/agentic.sh; timeout=10m; coordinator_agentic_preflight true'

.PHONY: coordinator-agentic-smoke
coordinator-agentic-smoke:
	@bash ops/validation/coordinator_agentic_smoke.sh

.PHONY: coordinator-agentic-autoscale
coordinator-agentic-autoscale:
	@bash ops/validation/coordinator_agentic_autoscale.sh

.PHONY: coordinator-agentic-registry
coordinator-agentic-registry:
	@bash ops/validation/coordinator_agentic_registry_smoke.sh
