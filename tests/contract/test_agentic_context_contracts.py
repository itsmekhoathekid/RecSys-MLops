from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

mcp = pytest.importorskip("mcp", reason="agentic CI profile owns MCP dependencies")

ROOT = Path(__file__).resolve().parents[2]
MCP_SRC = ROOT / "apps/agentic/recsys-feature-rag-mcp/src"
sys.path.insert(0, str(MCP_SRC))

from recsys_feature_rag_mcp.server import TOOL_NAMES, create_mcp_server


class _UnusedClient:
    """The contract test only needs FastMCP's generated tool metadata."""


def _contract() -> dict[str, Any]:
    return json.loads(
        (ROOT / "configs/agentic/recsys-context-agent/tools-contract.json").read_text(
            encoding="utf-8"
        )
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


def _render(chart: str) -> list[dict[str, Any]]:
    output = subprocess.run(
        ["helm", "template", "contract-test", str(ROOT / "infra/helm" / chart)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [document for document in yaml.safe_load_all(output) if document]


def _resource(documents: list[dict[str, Any]], kind: str, name: str):
    return next(
        item
        for item in documents
        if item.get("kind") == kind and item.get("metadata", {}).get("name") == name
    )


def test_mcp_tools_list_and_generated_input_schemas_match_contract():
    contract = _contract()
    server = create_mcp_server(_UnusedClient(), _UnusedClient())
    tools = asyncio.run(server.list_tools())

    assert list(TOOL_NAMES) == contract["tools"]
    assert [tool.name for tool in tools] == contract["tools"]
    assert {
        tool.name: _without_titles(tool.inputSchema) for tool in tools
    } == contract["inputSchemas"]


def test_agent_and_sandbox_use_the_exact_remote_mcp_tool_contract():
    contract = _contract()
    documents = _render("recsys-kagent-agent")

    remote = _resource(documents, "RemoteMCPServer", contract["server"])
    assert remote["spec"]["protocol"] == "STREAMABLE_HTTP"
    assert remote["spec"]["url"].endswith(":8080/mcp")
    assert remote["spec"]["timeout"] == "10s"
    assert remote["spec"]["headersFrom"][0]["name"] == "Authorization"

    for kind, name in (
        ("Agent", "recsys-context-agent"),
        ("SandboxAgent", "recsys-context-agent-sandbox"),
    ):
        resource = _resource(documents, kind, name)
        tool_names = resource["spec"]["declarative"]["tools"][0]["mcpServer"][
            "toolNames"
        ]
        assert tool_names == contract["tools"]

    sandbox = _resource(
        documents, "SandboxAgent", "recsys-context-agent-sandbox"
    )
    assert sandbox["spec"]["declarative"]["runtime"] == "go"
    assert sandbox["spec"]["platform"] == "substrate"
    assert sandbox["spec"]["sandbox"]["network"]["allowedDomains"] == [
        "recsys-feature-rag-mcp.kagent.svc.cluster.local"
    ]
    assert sandbox["spec"]["substrate"]["workerPoolRef"]["name"] == (
        "recsys-context-sandbox-pool"
    )


def test_native_agentic_workload_contracts_are_safe_and_scalable():
    mcp_documents = _render("recsys-feature-rag-mcp")
    deployment = _resource(
        mcp_documents, "Deployment", "recsys-feature-rag-mcp"
    )
    strategy = deployment["spec"]["strategy"]
    assert strategy["rollingUpdate"] == {"maxUnavailable": 0, "maxSurge": 1}
    pod_spec = deployment["spec"]["template"]["spec"]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod_spec["securityContext"]["runAsNonRoot"] is True

    pdb = _resource(mcp_documents, "PodDisruptionBudget", "recsys-feature-rag-mcp")
    assert pdb["spec"]["minAvailable"] == 1
    scaled = _resource(mcp_documents, "ScaledObject", "recsys-feature-rag-mcp")
    assert scaled["spec"]["minReplicaCount"] == 2
    assert scaled["spec"]["maxReplicaCount"] == 6
    assert scaled["spec"]["fallback"] == {"failureThreshold": 3, "replicas": 2}
    assert not any(item.get("kind") == "Ingress" for item in mcp_documents)


def test_gcp_agentic_workloads_use_the_ml_system_pool():
    for chart, kind, name in (
        ("recsys-feature-rag-mcp", "Deployment", "recsys-feature-rag-mcp"),
        ("recsys-kagent-agent", "Agent", "recsys-context-agent"),
    ):
        output = subprocess.run(
            [
                "helm",
                "template",
                "contract-test",
                str(ROOT / "infra/helm" / chart),
                "-f",
                str(ROOT / "infra/helm" / chart / "values-gcp.yaml"),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        documents = [document for document in yaml.safe_load_all(output) if document]
        resource = _resource(documents, kind, name)
        if kind == "Deployment":
            pod_spec = resource["spec"]["template"]["spec"]
        else:
            pod_spec = resource["spec"]["declarative"]["deployment"]
            assert pod_spec["imageRegistry"] == "ghcr.io"
        assert pod_spec["nodeSelector"] == {"recsys.ai/pool": "ml-system"}
        assert pod_spec["tolerations"] == [
            {
                "key": "recsys.ai/workload",
                "operator": "Equal",
                "value": "ml-system",
                "effect": "NoSchedule",
            }
        ]


def test_terraform_owns_platform_but_not_the_agent_application_release():
    terraform = (ROOT / "infra/terraform/gcp/kagent.tf").read_text(encoding="utf-8")
    assert 'resource "helm_release" "substrate"' in terraform
    assert 'resource "kubernetes_persistent_volume_claim_v1" "substrate_rustfs"' in terraform
    assert 'resource "kubernetes_persistent_volume_claim_v1" "substrate_valkey"' in terraform
    assert 'storage_class_name = "standard"' in terraform
    assert "prevent_destroy = true" in terraform
    assert 'postrender {' in terraform
    assert "substrate_gke_postrender.py" in terraform
    assert 'resource "helm_release" "recsys_kagent_agent"' not in terraform


def test_vault_bootstrap_creates_the_mcp_bearer_secret_idempotently():
    bootstrap = (ROOT / "ops/gcp/bootstrap_vault.sh").read_text(encoding="utf-8")
    assert "agentregistry feature-rag-mcp" in bootstrap
    assert '"${secret_group}" == "feature-rag-mcp"' in bootstrap
    assert "MCP_AUTH_TOKEN" in bootstrap
    assert 'Authorization: ("Bearer " + $token)' in bootstrap
    assert "keeping the existing Vault version" in bootstrap
    values = (ROOT / "configs/kagent/values.yaml").read_text(encoding="utf-8")
    assert "replicas: 2" in values
    assert "ateom-gvisor:v0.0.6" in values
    assert "sandboxClass: gvisor" in values


def test_registry_uses_arctl_namespaced_mcp_identity():
    deploy = (ROOT / "jenkins/scripts/deploy/agentic.sh").read_text(
        encoding="utf-8"
    )
    assert "recsys/recsys-feature-rag-mcp" in deploy
    assert 'arctl mcp publish "${registry_name}"' in deploy
