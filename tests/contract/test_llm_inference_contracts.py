from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_qwen_llama_cpp_chart_is_cpu_scheduled_and_openai_compatible() -> None:
    values = (ROOT / "infra/helm/recsys-llm-serving/values-gcp.yaml").read_text()
    deployment = (
        ROOT / "infra/helm/recsys-llm-serving/templates/deployment.yaml"
    ).read_text()
    assert "ghcr.io/ggml-org/llama.cpp" in values
    assert "tag: server" in values
    assert "ggml-org/Qwen3.5-0.8B-GGUF" in values
    assert "quantization: Q4_0" in values
    assert "recsys.ai/workload: llm-inference" in values
    assert "--hf-repo" in deployment
    assert "--alias" in deployment
    assert "--ctx-size" in deployment
    assert "--n-predict" in deployment
    assert "--reasoning-budget" in deployment
    assert "--reasoning-budget-message" in deployment
    assert "maxPredictedTokens: 768" in values
    assert "reasoningBudget: 256" in values
    assert "--no-mmproj" in deployment
    assert "--metrics" in deployment


def test_llm_pool_is_quota_safe_and_scales_from_one_to_two() -> None:
    gke = (ROOT / "infra/terraform/gcp/gke.tf").read_text()
    variables = (ROOT / "infra/terraform/gcp/variables.tf").read_text()
    assert 'resource "google_container_node_pool" "llm_cpu"' in gke
    assert "disk_type       = var.llm_cpu_disk_type" in gke
    assert "max_surge       = 0" in gke
    assert 'default     = "pd-standard"' in variables
    assert 'variable "llm_cpu_min_nodes"' in variables
    max_nodes = variables.split('variable "llm_cpu_max_nodes"', 1)[1].split("}", 1)[0]
    assert "default     = 2" in max_nodes


def test_shared_cpu_node_profile_fits_the_live_quota_constrained_topology() -> None:
    shared = (
        ROOT / "infra/helm/recsys-llm-serving/values-cpu-shared.yaml"
    ).read_text()
    terraform = (ROOT / "infra/terraform/gcp/llm_inference.tf").read_text()
    gke = (ROOT / "infra/terraform/gcp/gke.tf").read_text()
    deployment = (
        ROOT / "infra/helm/recsys-llm-serving/templates/deployment.yaml"
    ).read_text()
    production = (ROOT / "infra/terraform/gcp/terraform.tfvars.example").read_text()
    assert "cloud.google.com/gke-nodepool: recsys-mlops-cpu" in shared
    assert "replicaCount: 2" in shared
    assert "cpu: 100m" in shared
    assert "memory: 1536Mi" in shared
    assert "contextSize: 16384" in shared
    assert "topologySpread:" in shared
    assert "whenUnsatisfiable: DoNotSchedule" in shared
    assert "topologySpreadConstraints:" in deployment
    assert 'llm_node_pool_mode   = "cpu-services-shared"' in production
    assert 'cpu_machine_type = "e2-standard-8"' in production
    assert "cpu_min_nodes    = 2" in production
    assert "cpu_max_nodes    = 2" in production
    assert 'ml_machine_type = "e2-standard-4"' in production
    assert "ml_min_nodes    = 1" in production
    assert "ml_max_nodes    = 1" in production
    assert "enable_gpu_pool       = false" in production
    assert 'var.llm_node_pool_mode == "cpu-services-shared"' in terraform
    assert (
        'var.deploy_llm_inference && var.llm_node_pool_mode == "dedicated"'
        in gke
    )


def test_agentgateway_and_llama_cpp_router_are_managed_by_terraform() -> None:
    terraform = (ROOT / "infra/terraform/gcp/llm_inference.tf").read_text()
    baseline = (
        ROOT / "configs/llm-d/router-llama-cpp-cpu-baseline-values.yaml"
    ).read_text()
    optimized = (
        ROOT / "configs/llm-d/router-llama-cpp-cpu-optimized-values.yaml"
    ).read_text()
    for release in (
        "agentgateway_crds",
        "agentgateway",
        "recsys_llm_serving",
        "llm_d_router",
    ):
        assert f'resource "helm_release" "{release}"' in terraform
    assert 'var.llm_optimization_profile == "optimized"' in terraform
    assert "random-picker" in baseline
    assert "prefix-cache" not in baseline
    assert "inflight-load" not in baseline
    assert "inflight-load-producer" in optimized
    assert "token-load-scorer" in optimized
    assert "prefix-cache" not in optimized


def test_kagent_global_model_config_routes_through_agentgateway() -> None:
    terraform = (ROOT / "infra/terraform/gcp/kagent.tf").read_text()
    values = (ROOT / "configs/kagent/values.yaml").read_text()
    agent = (
        ROOT / "infra/helm/recsys-kagent-agent/templates/sandboxagent.yaml"
    ).read_text()
    for release in ("kagent_crds", "kagent"):
        assert f'resource "helm_release" "{release}"' in terraform
    assert 'default     = "0.10.0-e6df917"' in terraform
    assert 'kagent_source_commit    = "e6df917e9fa8"' in terraform
    assert (
        'kagent_image_version    = "0.10.0-e6df917-substrate0011-v7"'
        in terraform
    )
    cloudbuild = (ROOT / "ops/gcp/cloudbuild_kagent_source.yaml").read_text()
    assert "build-push-controller" in cloudbuild
    assert "build-push-golang-adk" in cloudbuild
    assert "0.10.0-e6df917-substrate0011-v7" in cloudbuild
    compatibility_patch = (
        ROOT / "ops/gcp/patches/kagent-e6df917-substrate0011.patch"
    ).read_text()
    assert "TimeoutSeconds: 30" in compatibility_patch
    assert "ResumeSourceColdBoot" in compatibility_patch
    assert "newDuplicateToolCallGuard" in compatibility_patch
    assert "duplicateToolGuard.BeforeModel" in compatibility_patch
    assert "newExplicitToolSelectionGuard" in compatibility_patch
    assert "Suppressing duplicate tool-call loop" in compatibility_patch
    assert "desired spec must match the Substrate CRD default" in compatibility_patch
    assert '"kagent.dev/worker-pool"' in terraform
    assert '"recsys-recommendation-sandbox-pool"' in terraform
    assert '"recsys-coordinator-sandbox-pool"' in terraform
    assert "helm_release.llm_d_router" in terraform
    assert "model: qwen3.5-0.8b" in values
    assert "provider: OpenAI" in values
    assert "X-Gateway-Base-Model-Name: llm-d-optimized-baseline" in values
    assert (
        "baseUrl: "
        "http://llm-d-inference-gateway.llm-inference.svc.cluster.local/v1"
        in values
    )
    assert "maxTokens: 384" in values
    assert 'temperature: "0"' in values
    assert "seed: 42" in values
    assert "tls:" not in values
    assert 'resource "helm_release" "recsys_kagent_agent"' not in terraform
    assert 'resource "helm_release" "substrate"' in terraform
    assert "modelConfig: {{ .Values.sandbox.modelConfig }}" in agent
    assert "k8s-agent:\n  enabled: false" in values


def test_agentgateway_auth_uses_one_vault_key_for_client_and_server() -> None:
    policy = (
        ROOT / "infra/helm/recsys-llm-serving/templates/gateway-auth.yaml"
    ).read_text()
    security_values = (ROOT / "infra/helm/recsys-security/values.yaml").read_text()
    bootstrap = (ROOT / "ops/gcp/bootstrap_vault.sh").read_text()
    smoke = (ROOT / "ops/validation/llm_inference_smoke.sh").read_text()

    assert "kind: AgentgatewayPolicy" in policy
    assert "phase: PreRouting" in policy
    assert "mode: {{ .Values.gateway.auth.mode }}" in policy
    assert "secretName: kagent-agent-gateway" in security_values
    assert "secretName: agentgateway-api-keys" in security_values
    assert security_values.count("vaultPath: agent-gateway") == 2
    assert "AGENT_GATEWAY_API_KEY" in bootstrap
    assert 'write "recsys/data/${secret_group}"' in bootstrap
    assert 'unauthenticated_status' in smoke
    assert 'Authorization: Bearer ${GATEWAY_API_KEY}' in smoke


def test_llm_treatment_overlays_select_baseline_and_optimized_runtime() -> None:
    baseline = (
        ROOT / "infra/helm/recsys-llm-serving/values-baseline.yaml"
    ).read_text()
    optimized = (
        ROOT / "infra/helm/recsys-llm-serving/values-optimized.yaml"
    ).read_text()
    variables = (ROOT / "infra/terraform/gcp/variables.tf").read_text()
    assert "optimizationProfile: baseline" in baseline
    assert "load" not in baseline.split("optimizationProfile:", 1)[1]
    assert "optimizationProfile: load-aware" in optimized
    assert 'default     = "baseline"' in variables


def test_gateway_api_crds_are_pinned_and_installed_before_agentgateway() -> None:
    terraform = (ROOT / "infra/terraform/gcp/llm_inference.tf").read_text()
    variables = (ROOT / "infra/terraform/gcp/variables.tf").read_text()
    installer = (ROOT / "ops/gcp/install_llm_gateway_crds.sh").read_text()
    assert 'resource "null_resource" "llm_gateway_api_crds"' in terraform
    assert "null_resource.llm_gateway_api_crds" in terraform
    assert 'default     = "v1.5.1"' in variables
    assert 'default     = "v1.5.0"' in variables
    assert "standard-install.yaml" in installer
    assert "v1-manifests.yaml" in installer
    assert "inferencepools.inference.networking.k8s.io" in installer
    assert "inference.networking.x-k8s.io" not in installer


def test_llm_operations_scripts_have_valid_shell_syntax() -> None:
    for relative in (
        "ops/validation/llm_inference_smoke.sh",
        "ops/validation/llm_inference_benchmark.sh",
        "ops/gcp/services_power.sh",
    ):
        subprocess.run(
            ["bash", "-n", str(ROOT / relative)],
            cwd=ROOT,
            check=True,
        )


def test_power_management_tracks_the_llm_pool() -> None:
    script = (ROOT / "ops/gcp/services_power.sh").read_text()
    assert "recsys-mlops-llm-cpu" in script
    assert "record_pool_state LLM_CPU" in script
    assert 'scale_pool_down LLM_CPU "${LLM_CPU_NODE_POOL}"' in script
    assert 'scale_pool_up LLM_CPU "${LLM_CPU_POOL}"' in script
    assert 'DEFAULT_CPU_NODES="${GCP_CPU_NODES:-2}"' in script
    assert 'DEFAULT_CPU_MAX_NODES="${GCP_CPU_MAX_NODES:-2}"' in script
    assert 'DEFAULT_ML_NODES="${GCP_ML_NODES:-1}"' in script
    assert 'DEFAULT_ML_MAX_NODES="${GCP_ML_MAX_NODES:-1}"' in script
    assert 'DEFAULT_LLM_CPU_NODES="${GCP_LLM_CPU_NODES:-0}"' in script
    assert 'DEFAULT_LLM_CPU_MAX_NODES="${GCP_LLM_CPU_MAX_NODES:-0}"' in script
