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


def _resource(
    documents: list[dict[str, Any]], kind: str, name: str
) -> dict[str, Any]:
    return next(
        item
        for item in documents
        if item.get("kind") == kind and item.get("metadata", {}).get("name") == name
    )


def test_coordinator_references_two_agents_and_two_existing_mcp_servers() -> None:
    documents = _render()
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert sum(item.get("kind") == "SandboxAgent" for item in documents) == 1
    assert sum(item.get("kind") == "ScaledObject" for item in documents) == 1
    assert sum(item.get("kind") == "PodDisruptionBudget" for item in documents) == 1
    assert not any(item.get("kind") == "RemoteMCPServer" for item in documents)

    sandbox = _resource(
        documents, "SandboxAgent", "recsys-coordinator-agent-sandbox"
    )
    tools = sandbox["spec"]["declarative"]["tools"]
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
    assert sum(len(item["toolNames"]) for item in mcp_tools) == 5
    assert sandbox["spec"]["sandbox"]["network"]["allowedDomains"] == [
        "kagent-controller.kagent",
        "recsys-feature-rag-mcp.kagent.svc.cluster.local",
        "recsys-recommendation-mcp.kagent.svc.cluster.local",
    ]


def test_coordinator_prompt_locks_routing_grounding_and_partial_results() -> None:
    sandbox = _resource(
        _render(), "SandboxAgent", "recsys-coordinator-agent-sandbox"
    )
    prompt = sandbox["spec"]["declarative"]["systemMessage"]
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
    ):
        assert requirement in prompt
    skills = sandbox["spec"]["declarative"]["a2aConfig"]["skills"]
    assert [skill["id"] for skill in skills] == [
        "coordinated-personalized-recommendation"
    ]


def test_coordinator_workerpool_scales_one_to_three_with_fallback_one() -> None:
    documents = _render("values-gcp.yaml")
    scaled = _resource(
        documents, "ScaledObject", "recsys-coordinator-sandbox-pool"
    )["spec"]
    assert scaled["scaleTargetRef"] == {
        "apiVersion": "ate.dev/v1alpha1",
        "kind": "WorkerPool",
        "name": "recsys-coordinator-sandbox-pool",
    }
    assert (scaled["minReplicaCount"], scaled["maxReplicaCount"]) == (1, 3)
    assert scaled["fallback"] == {"failureThreshold": 3, "replicas": 1}
    assert scaled["pollingInterval"] == 15
    assert scaled["cooldownPeriod"] == 120
    assert scaled["advanced"]["horizontalPodAutoscalerConfig"]["behavior"][
        "scaleDown"
    ]["stabilizationWindowSeconds"] == 60
    assert scaled["triggers"][0]["metadata"]["threshold"] == "400"
    assert "recsys-coordinator-sandbox-pool-deployment-.*" in scaled["triggers"][0][
        "metadata"
    ]["query"]
    pdb = _resource(
        documents, "PodDisruptionBudget", "recsys-coordinator-sandbox-pool"
    )
    assert pdb["spec"]["minAvailable"] == 1


def test_terraform_owns_dedicated_coordinator_pool_and_ignores_replica_drift() -> None:
    terraform = (ROOT / "infra/terraform/gcp/kagent.tf").read_text(encoding="utf-8")
    block = terraform.split(
        'resource "kubernetes_manifest" "recsys_coordinator_sandbox_pool"', 1
    )[1].split(
        'resource "kubernetes_cluster_role_v1" "keda_workerpool_scaler"', 1
    )[0]
    assert 'name      = "recsys-coordinator-sandbox-pool"' in block
    assert 'replicas      = 1' in block
    assert "ateom-gvisor:v${var.agent_substrate_version}" in block
    assert 'scaleSelector = "ate.dev/worker-pool=recsys-coordinator-sandbox-pool"' in block
    assert 'computed_fields = ["spec.replicas"]' in block


def test_registry_manifest_records_mcp_and_a2a_dependencies(
    tmp_path: Path,
) -> None:
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
    assert manifest["metadata"]["annotations"]["recsys.dev/a2a-dependencies"] == (
        "recsys/recsys-context-agent-sandbox@0.1.0-0123456789ab,"
        "recsys/recsys-recommendation-agent-sandbox@0.1.0-0123456789ab"
    )
    assert [item["name"] for item in manifest["spec"]["mcpServers"]] == [
        "recsys-feature-rag-mcp",
        "recsys-recommendation-mcp",
    ]


def test_coordinator_ci_and_deploy_dependencies_are_wired() -> None:
    components = json.loads(
        (ROOT / "jenkins/config/components.json").read_text(encoding="utf-8")
    )["components"]
    coordinator = next(item for item in components if item["name"] == "coordinator_agent")
    assert coordinator["buildImages"] == []
    assert coordinator["verifyDependsOn"] == [
        "context_agent",
        "recommendation_agent",
    ]
    units = {
        item["name"]: item
        for item in json.loads(
            (ROOT / "jenkins/config/deploy-units.json").read_text(encoding="utf-8")
        )["units"]
    }
    assert units["coordinator-agent"]["dependsOn"] == [
        "context-agent",
        "recommendation-agent",
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
