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
    assert sum(item.get("kind") == "Agent" for item in documents) == 1
    assert not any(item.get("kind") == "SandboxAgent" for item in documents)
    assert not any(item.get("kind") == "ScaledObject" for item in documents)
    assert not any(item.get("kind") == "PodDisruptionBudget" for item in documents)
    assert not any(item.get("kind") == "RemoteMCPServer" for item in documents)

    agent = _resource(documents, "Agent", "recsys-coordinator-agent")
    assert agent["spec"]["declarative"]["deployment"]["replicas"] == 1
    assert "sandbox" not in agent["spec"]
    assert "substrate" not in agent["spec"]
    tools = agent["spec"]["declarative"]["tools"]
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


def test_coordinator_prompt_locks_routing_grounding_and_partial_results() -> None:
    agent = _resource(_render(), "Agent", "recsys-coordinator-agent")
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
    ):
        assert requirement in prompt
    skills = agent["spec"]["declarative"]["a2aConfig"]["skills"]
    assert [skill["id"] for skill in skills] == [
        "coordinated-personalized-recommendation"
    ]


def test_coordinator_is_a_fixed_single_replica_regular_agent() -> None:
    documents = _render("values-gcp.yaml")
    agent = _resource(documents, "Agent", "recsys-coordinator-agent")
    assert agent["spec"]["declarative"]["deployment"] == {
        "replicas": 1,
        "imageRegistry": "ghcr.io",
    }
    assert {document["kind"] for document in documents} == {"Agent"}


def test_terraform_no_longer_owns_a_coordinator_workerpool() -> None:
    terraform = (ROOT / "infra/terraform/gcp/kagent.tf").read_text(encoding="utf-8")
    assert "recsys_coordinator_sandbox_pool" not in terraform
    assert "recsys-coordinator-sandbox-pool" not in terraform


def test_registry_manifest_records_mcp_and_a2a_dependencies(
    tmp_path: Path,
) -> None:
    script = r'''
set -Eeuo pipefail
source jenkins/scripts/deploy/agentic.sh
agentic_write_registry_manifest "$1" coordinator-agent \
  recsys/recsys-coordinator-agent \
  0.1.0+0123456789ab 0.1.0-0123456789ab \
  0123456789abcdef0123456789abcdef01234567 \
  https://example.invalid/recsys.git
'''
    output = tmp_path / "coordinator-registry.json"
    subprocess.run(["bash", "-c", script, "bash", str(output)], cwd=ROOT, check=True)
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["metadata"]["name"] == "recsys-coordinator-agent"
    assert manifest["metadata"]["labels"]["recsys.dev/variant"] == "regular"
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
        "ops/validation/coordinator_agentic_concurrency.sh",
        "ops/validation/coordinator_agentic_registry_smoke.sh",
    ):
        subprocess.run(
            ["bash", "-n", str(ROOT / relative_path)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
