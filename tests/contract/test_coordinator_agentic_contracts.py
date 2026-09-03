from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/recsys-coordinator-agent"
CONTRACT = ROOT / "configs/agentic/recsys-coordinator-agent/tools-contract.json"


def _render(values: str | None = None) -> list[dict[str, Any]]:
    command = ["helm", "template", "contract-test", str(CHART)]
    if values:
        command.extend(["-f", str(CHART / values)])
    output = subprocess.run(
        command, cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return [document for document in yaml.safe_load_all(output) if document]


def _resource(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(
        item
        for item in documents
        if item.get("kind") == kind and item.get("metadata", {}).get("name") == name
    )


def test_coordinator_sandbox_references_two_agents_and_two_mcp_servers() -> None:
    documents = _render()
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert sum(item.get("kind") == "SandboxAgent" for item in documents) == 1
    assert not any(item.get("kind") == "Agent" for item in documents)
    assert sum(item.get("kind") == "ScaledObject" for item in documents) == 1
    assert sum(item.get("kind") == "PodDisruptionBudget" for item in documents) == 1
    assert not any(item.get("kind") == "RemoteMCPServer" for item in documents)

    agent = _resource(documents, "SandboxAgent", "recsys-coordinator-agent-sandbox")
    assert agent["apiVersion"] == "kagent.dev/v1alpha3"
    assert "platform" not in agent["spec"]
    assert agent["spec"]["substrate"]["workerPoolRef"]["name"] == (
        "recsys-coordinator-sandbox-pool"
    )
    tools = agent["spec"]["declarative"]["tools"]
    assert all(
        item.get("isolateSessions") is True
        for item in tools
        if item["type"] == "Agent"
    )
    agent_tools = [item["agent"] for item in tools if item["type"] == "Agent"]
    mcp_tools = [item["mcpServer"] for item in tools if item["type"] == "McpServer"]
    assert agent_tools == contract["agents"]
    assert [
        {
            "apiGroup": item["apiGroup"],
            "kind": item["kind"],
            "name": item["name"],
            "tools": item["toolNames"],
        }
        for item in mcp_tools
    ] == contract["mcpServers"]
    # The Coordinator exposes only two raw verification tools. Feature/RAG
    # aggregation stays behind the Context specialist so generic requests
    # cannot bypass A2A routing.
    assert sum(len(item["toolNames"]) for item in mcp_tools) == 2


def test_coordinator_prompt_locks_routing_grounding_and_partial_results() -> None:
    agent = _resource(_render(), "SandboxAgent", "recsys-coordinator-agent-sandbox")
    prompt = agent["spec"]["declarative"]["systemMessage"]
    for requirement in (
        "smallest",
        "delegate to the context agent",
        "delegate to the recommendation agent",
        "Call MCP tools directly only",
        "second independent",
        "Never rerank",
        "chunk_id",
        "partial results",
        "Do not retry",
        "only allowed",
        "Never forward the original composite user prompt",
        "permits exactly one call to each function",
        "MUST contain only this JSON object",
        "must not call ask_user",
        "Recommended item_id: <first returned item_id>",
    ):
        assert requirement in prompt
    assert (
        agent["metadata"]["annotations"]["recsys.ai/model-config-revision"]
        == "substrate-0.0.11-kagent-e6df917-assigned-workers-v22"
    )
    skills = agent["spec"]["declarative"]["a2aConfig"]["skills"]
    assert [skill["id"] for skill in skills] == [
        "coordinated-personalized-recommendation"
    ]


def test_production_coordinator_uses_assigned_worker_autoscaling() -> None:
    documents = _render("values-gcp.yaml")
    agent = _resource(documents, "SandboxAgent", "recsys-coordinator-agent-sandbox")
    assert "deployment" not in agent["spec"]["declarative"]
    scaled = _resource(documents, "ScaledObject", "recsys-coordinator-sandbox-pool")
    spec = scaled["spec"]
    assert spec["scaleTargetRef"] == {
        "apiVersion": "ate.dev/v1alpha1",
        "kind": "WorkerPool",
        "name": "recsys-coordinator-sandbox-pool",
    }
    assert (spec["minReplicaCount"], spec["maxReplicaCount"]) == (1, 3)
    assert (spec["pollingInterval"], spec["cooldownPeriod"]) == (15, 300)
    assert spec["fallback"] == {"failureThreshold": 3, "replicas": 1}
    trigger = spec["triggers"][0]
    assert trigger["metricType"] == "AverageValue"
    assert trigger["metadata"] == {
        "serverAddress": "http://recsys-prometheus.observability.svc.cluster.local:9090",
        "ignoreNullValues": "false",
        "metricName": "recsys_coordinator_sandbox_assigned_workers",
        "threshold": "0.7",
        "query": (
            'max(ate_workerpool_workers{ate_workerpool_namespace="kagent",'
            'ate_workerpool_name="recsys-coordinator-sandbox-pool",'
            'ate_worker_state="assigned"})'
        ),
    }
    behavior = spec["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]
    assert behavior["scaleDown"]["stabilizationWindowSeconds"] == 300
    assert behavior["scaleUp"] == {
        "stabilizationWindowSeconds": 0,
        "selectPolicy": "Max",
        "policies": [
            {"type": "Percent", "value": 100, "periodSeconds": 15},
            {"type": "Pods", "value": 10, "periodSeconds": 15},
        ],
    }


def test_terraform_owns_coordinator_pool_and_lets_keda_manage_replicas() -> None:
    terraform = (ROOT / "infra/terraform/gcp/modules/kubernetes-platform/kagent.tf").read_text(encoding="utf-8")
    assert 'resource "kubernetes_manifest" "recsys_coordinator_sandbox_pool"' in terraform
    assert 'name      = "recsys-coordinator-sandbox-pool"' in terraform
    assert 'computed_fields = ["spec.replicas"]' in terraform
    assert '"ate.dev/worker-pool"' in terraform
    assert "scaleSelector" not in terraform


def test_registry_manifest_records_sandbox_a2a_dependencies(tmp_path: Path) -> None:
    script = r'''
set -Eeuo pipefail
source jenkins/scripts/deploy/agentic.sh
agentic_write_registry_manifest "$1" coordinator-agent \
  recsys/recsys-coordinator-agent-sandbox \
  0.1.0+0123456789ab 0.1.0-0123456789ab \
  0123456789abcdef0123456789abcdef01234567 \
  https://example.invalid/recsys.git
'''
    output = tmp_path / "coordinator-registry.json"
    subprocess.run(["bash", "-c", script, "bash", str(output)], cwd=ROOT, check=True)
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["metadata"]["name"] == "recsys-coordinator-agent-sandbox"
    assert manifest["metadata"]["labels"]["recsys.dev/variant"] == "sandbox"
    assert manifest["metadata"]["annotations"]["recsys.dev/a2a-dependencies"] == (
        "recsys/recsys-context-agent-sandbox@0.1.0-0123456789ab,"
        "recsys/recsys-recommendation-agent-sandbox@0.1.0-0123456789ab"
    )
    assert [item["name"] for item in manifest["spec"]["mcpServers"]] == [
        "recsys-feature-rag-mcp",
        "recsys-recommendation-mcp",
    ]


def test_coordinator_ci_and_deploy_dependencies_are_wired() -> None:
    deploy_script = (ROOT / "jenkins/scripts/deploy/agentic.sh").read_text(
        encoding="utf-8"
    )
    assert "Pass the Recommendation Agent exactly this complete JSON request" in (
        deploy_script
    )
    assert 'candidate_item_ids\\\":null,\\\"top_k\\\":1' in deploy_script
    assert 'COORDINATOR_A2A_REQUEST_TIMEOUT_SECONDS:-1800' in deploy_script
    assert 'COORDINATOR_A2A_MAX_ATTEMPTS:-1' in deploy_script
    assert '"http_422"' in deploy_script
    assert "assert_usable_agent_response" in deploy_script
    assert "Recommended item_id: <first returned item_id>" in deploy_script
    coordinator_smoke = deploy_script.split("coordinator_a2a_smoke()", 1)[1].split(
        "agentic_mcp_protocol_smoke()", 1
    )[0]
    assert 'for attempt in $(seq 1 "${max_attempts}")' in coordinator_smoke
    assert "for attempt in 1 2 3" not in coordinator_smoke
    components = json.loads(
        (ROOT / "jenkins/config/components.json").read_text(encoding="utf-8")
    )["components"]
    coordinator = next(item for item in components if item["name"] == "coordinator_agent")
    assert coordinator["buildImages"] == []
    assert coordinator["verifyDependsOn"] == ["context_agent", "recommendation_agent"]
    assert (
        "ops/validation/coordinator_agentic_autoscale.sh"
        in coordinator["changeDetection"]["files"]
    )
    units = {
        item["name"]: item
        for item in json.loads(
            (ROOT / "jenkins/config/deploy-units.json").read_text(encoding="utf-8")
        )["units"]
    }
    assert units["coordinator-agent"]["dependsOn"] == [
        "context-agent-registry",
        "recommendation-agent-registry",
    ]
    assert units["coordinator-agent-registry"]["dependsOn"] == [
        "coordinator-agent",
        "context-agent-registry",
        "recommendation-agent-registry",
        "feature-rag-mcp-registry",
        "recommendation-mcp-registry",
    ]


def test_coordinator_shell_entrypoints_are_syntactically_valid() -> None:
    for relative_path in (
        "jenkins/scripts/ci/agentic.sh",
        "jenkins/scripts/deploy/agentic.sh",
        "jenkins/scripts/test/agentic.sh",
        "ops/validation/coordinator_agentic_smoke.sh",
        "ops/validation/coordinator_agentic_autoscale.sh",
        "ops/validation/coordinator_agentic_registry_smoke.sh",
    ):
        subprocess.run(
            ["bash", "-n", str(ROOT / relative_path)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    a2a_generators = "\n".join(
        (ROOT / relative_path).read_text(encoding="utf-8")
        for relative_path in (
            "jenkins/scripts/deploy/agentic.sh",
            "ops/validation/coordinator_agentic_autoscale.sh",
            "ops/validation/agentic_autoscale_capture.sh",
        )
    )
    assert '"role": "ROLE_USER"' in a2a_generators
    assert '"role": "user"' not in a2a_generators
