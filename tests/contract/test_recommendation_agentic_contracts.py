from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("mcp", reason="recommendation-agentic profile owns MCP")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(
    0, str(ROOT / "apps/agentic/recsys-recommendation-mcp/src")
)
server = importlib.import_module("recsys_recommendation_mcp.server")


def _contract() -> dict[str, Any]:
    return json.loads(
        (
            ROOT
            / "configs/agentic/recsys-recommendation-agent/tools-contract.json"
        ).read_text(encoding="utf-8")
    )


def _without_titles(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_titles(item)
            for key, item in value.items()
            if key != "title"
        }
    if isinstance(value, list):
        return [_without_titles(item) for item in value]
    return value


def _render(chart: str, values: str | None = None) -> list[dict[str, Any]]:
    command = ["helm", "template", "contract-test", str(ROOT / "infra/helm" / chart)]
    if values:
        command.extend(["-f", str(ROOT / "infra/helm" / chart / values)])
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


def test_fastmcp_schema_matches_the_single_versioned_contract() -> None:
    contract = _contract()
    tools = asyncio.run(server.create_mcp_server(object()).list_tools())
    assert [tool.name for tool in tools] == [
        item["name"] for item in contract["tools"]
    ] == ["get_personalized_recommendations"]
    assert _without_titles(tools[0].inputSchema) == contract["tools"][0]["inputSchema"]


def test_agent_has_only_recommendation_mcp_and_no_agent_dependency() -> None:
    contract = _contract()
    documents = _render("recsys-recommendation-agent")
    rendered = yaml.safe_dump_all(documents)

    assert not any(item.get("kind") == "Agent" for item in documents)
    assert sum(item.get("kind") == "SandboxAgent" for item in documents) == 1
    assert sum(item.get("kind") == "RemoteMCPServer" for item in documents) == 1
    assert "recsys-context-agent" not in rendered
    assert "recsys-feature-rag-mcp" not in rendered

    sandbox = _resource(
        documents, "SandboxAgent", "recsys-recommendation-agent-sandbox"
    )
    tools = sandbox["spec"]["declarative"]["tools"]
    assert len(tools) == 1
    assert tools[0]["type"] == "McpServer"
    assert tools[0]["mcpServer"]["name"] == contract["server"]
    assert tools[0]["mcpServer"]["toolNames"] == [
        item["name"] for item in contract["tools"]
    ]
    assert sandbox["spec"]["sandbox"]["network"]["allowedDomains"] == [
        "recsys-recommendation-mcp.kagent.svc.cluster.local"
    ]


def test_mcp_and_workerpool_scale_one_to_three_with_fallback_one() -> None:
    mcp_documents = _render("recsys-recommendation-mcp")
    deployment = _resource(
        mcp_documents, "Deployment", "recsys-recommendation-mcp"
    )
    assert deployment["spec"]["strategy"]["rollingUpdate"] == {
        "maxUnavailable": 0,
        "maxSurge": 1,
    }
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    mcp_scaled = _resource(
        mcp_documents, "ScaledObject", "recsys-recommendation-mcp"
    )
    assert (
        mcp_scaled["spec"]["minReplicaCount"],
        mcp_scaled["spec"]["maxReplicaCount"],
        mcp_scaled["spec"]["fallback"]["replicas"],
    ) == (1, 3, 1)

    agent_documents = _render("recsys-recommendation-agent")
    worker_scaled = _resource(
        agent_documents, "ScaledObject", "recsys-recommendation-sandbox-pool"
    )
    assert worker_scaled["spec"]["scaleTargetRef"] == {
        "apiVersion": "ate.dev/v1alpha1",
        "kind": "WorkerPool",
        "name": "recsys-recommendation-sandbox-pool",
    }
    assert (
        worker_scaled["spec"]["minReplicaCount"],
        worker_scaled["spec"]["maxReplicaCount"],
        worker_scaled["spec"]["fallback"]["replicas"],
    ) == (1, 3, 1)
    pdb = _resource(
        agent_documents, "PodDisruptionBudget", "recsys-recommendation-sandbox-pool"
    )
    assert pdb["spec"]["selector"]["matchLabels"] == {
        "ate.dev/worker-pool": "recsys-recommendation-sandbox-pool"
    }


def test_terraform_owns_dedicated_pool_and_ignores_keda_replica_drift() -> None:
    terraform = (ROOT / "infra/terraform/gcp/kagent.tf").read_text(encoding="utf-8")
    assert 'resource "kubernetes_manifest" "recsys_recommendation_sandbox_pool"' in terraform
    assert 'computed_fields = ["spec.replicas"]' in terraform
    assert 'scaleSelector = "ate.dev/worker-pool=recsys-recommendation-sandbox-pool"' in terraform
    assert 'name      = "keda-operator"' in terraform
    assert 'resources  = ["workerpools/scale"]' in terraform


def test_jenkins_preflight_verifies_keda_rbac_without_impersonated_ssar() -> None:
    deploy = (ROOT / "jenkins/scripts/deploy/agentic.sh").read_text(encoding="utf-8")
    recommendation_preflight = deploy.split(
        "recommendation_agentic_preflight()", maxsplit=1
    )[1].split("recommendation_mcp_protocol_smoke()", maxsplit=1)[0]

    assert "kubectl auth can-i" not in recommendation_preflight
    assert "clusterrole keda-ate-workerpool-scaler" in recommendation_preflight
    assert "clusterrolebinding keda-ate-workerpool-scaler" in recommendation_preflight
    assert '"workerpools/scale"' in recommendation_preflight
    assert '{"get", "patch", "update"}' in recommendation_preflight


def test_autoscale_and_smoke_proof_scripts_are_portable_and_bounded() -> None:
    paths = [
        ROOT / "ops/validation/recommendation_agentic_autoscale.sh",
        ROOT / "ops/validation/recommendation_agentic_smoke.sh",
    ]
    for path in paths:
        subprocess.run(
            ["bash", "-n", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )

    autoscale = paths[0].read_text(encoding="utf-8")
    assert "%(" not in autoscale
    assert 'wait "${mcp_load_pid}"' in autoscale
    assert 'wait "${worker_load_pid}"' in autoscale
    assert "RECOMMENDATION_MCP_LOAD_CONCURRENCY" in autoscale
    assert "ThreadPoolExecutor(max_workers=4)" in autoscale

    smoke = paths[1].read_text(encoding="utf-8")
    assert "remotemcpserver/recsys-recommendation-mcp" in smoke
    assert "sandboxagent/recsys-recommendation-agent-sandbox" in smoke
    assert "workerpool/recsys-recommendation-sandbox-pool" in smoke
