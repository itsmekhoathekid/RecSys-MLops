SHELL := /bin/bash
UV_CACHE_DIR ?= .uv-cache
export UV_CACHE_DIR

GCP_POWER_SCRIPT := ops/gcp/services_power.sh
GCP_VERIFY_SCRIPT := ops/validation/verify_gcp_stack.sh
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
	@echo ""
	@echo "Artifacts and operations:"
	@echo "  make compile-kfp              Compile the BST Kubeflow pipeline package"
	@echo "  make gcp-services-down        Hibernate GCP services while preserving PVC data"
	@echo "  make gcp-services-up          Restore GCP services and run smoke checks"
	@echo "  make gcp-services-status      Show GCP service and node-pool status"
	@echo "  make serving-autoscale-load-test  Run production serving load validation"
	@echo "  make llm-inference-smoke          Validate Qwen vLLM CPU, llm-d, and agentgateway"
	@echo "  make llm-inference-benchmark      Benchmark the deployed gateway endpoint"
	@echo "  make jenkins-full             Trigger the full production Jenkins CI/CD job"

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
	  if [[ -f "$${chart_dir}/values-gcp.yaml" ]]; then \
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
