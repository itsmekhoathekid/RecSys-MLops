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

mcp = pytest.importorskip("mcp", reason="agentic CI profile owns MCP dependencies")

ROOT = Path(__file__).resolve().parents[2]
MCP_SRC = ROOT / "apps/agentic/recsys-feature-rag-mcp/src"
sys.path.insert(0, str(MCP_SRC))

server_module = importlib.import_module("recsys_feature_rag_mcp.server")
TOOL_NAMES = server_module.TOOL_NAMES
create_mcp_server = server_module.create_mcp_server


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


def test_sandbox_uses_the_exact_remote_mcp_tool_contract():
    contract = _contract()
    documents = _render("recsys-kagent-agent")

    remote = _resource(documents, "RemoteMCPServer", contract["server"])
    assert remote["spec"]["protocol"] == "STREAMABLE_HTTP"
    assert remote["spec"]["url"].endswith(":8080/mcp")
    assert remote["spec"]["timeout"] == "10s"
    assert remote["spec"]["headersFrom"][0]["name"] == "Authorization"

    assert not any(item.get("kind") == "Agent" for item in documents)

    sandbox = _resource(
        documents, "SandboxAgent", "recsys-context-agent-sandbox"
    )
    tool_names = sandbox["spec"]["declarative"]["tools"][0]["mcpServer"][
        "toolNames"
    ]
    assert tool_names == contract["tools"]
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

    sandbox_documents = _render("recsys-kagent-agent")
    assert not any(item.get("kind") == "Agent" for item in sandbox_documents)
    assert sum(item.get("kind") == "SandboxAgent" for item in sandbox_documents) == 1
    assert sum(item.get("kind") == "RemoteMCPServer" for item in sandbox_documents) == 1
    sandbox_scaled = _resource(
        sandbox_documents, "ScaledObject", "recsys-context-sandbox-pool"
    )
    assert sandbox_scaled["spec"]["scaleTargetRef"] == {
        "apiVersion": "ate.dev/v1alpha1",
        "kind": "WorkerPool",
        "name": "recsys-context-sandbox-pool",
    }
    assert sandbox_scaled["spec"]["minReplicaCount"] == 2
    assert sandbox_scaled["spec"]["maxReplicaCount"] == 6
    assert sandbox_scaled["spec"]["fallback"] == {
        "failureThreshold": 3,
        "replicas": 2,
    }
    trigger = sandbox_scaled["spec"]["triggers"][0]["metadata"]
    assert trigger["metricName"] == "recsys_context_sandbox_worker_cpu_cores"
    assert 'container="ateom"' in trigger["query"]
    assert "recsys-context-sandbox-pool-deployment-.*" in trigger["query"]
    sandbox_pdb = _resource(
        sandbox_documents, "PodDisruptionBudget", "recsys-context-sandbox-pool"
    )
    assert sandbox_pdb["spec"] == {
        "minAvailable": 1,
        "selector": {
            "matchLabels": {
                "ate.dev/worker-pool": "recsys-context-sandbox-pool"
            }
        },
    }


def test_gcp_mcp_uses_ml_system_pool_and_sandbox_has_no_fake_scheduling_fields():
    for chart, kind, name in (
        ("recsys-feature-rag-mcp", "Deployment", "recsys-feature-rag-mcp"),
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
        pod_spec = resource["spec"]["template"]["spec"]
        assert pod_spec["nodeSelector"] == {"recsys.ai/pool": "ml-system"}
        assert pod_spec["tolerations"] == [
            {
                "key": "recsys.ai/workload",
                "operator": "Equal",
                "value": "ml-system",
                "effect": "NoSchedule",
            }
        ]

    sandbox_documents = [
        document
        for document in yaml.safe_load_all(
            subprocess.run(
                [
                    "helm",
                    "template",
                    "contract-test",
                    str(ROOT / "infra/helm/recsys-kagent-agent"),
                    "-f",
                    str(ROOT / "infra/helm/recsys-kagent-agent/values-gcp.yaml"),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        if document
    ]
    sandbox = _resource(
        sandbox_documents, "SandboxAgent", "recsys-context-agent-sandbox"
    )
    deployment = sandbox["spec"]["declarative"]["deployment"]
    assert deployment == {"imageRegistry": "ghcr.io"}


def test_terraform_owns_platform_but_not_the_agent_application_release():
    terraform = (ROOT / "infra/terraform/gcp/kagent.tf").read_text(encoding="utf-8")
    assert 'resource "helm_release" "substrate"' in terraform
    assert 'resource "kubernetes_persistent_volume_claim_v1" "substrate_rustfs"' in terraform
    assert 'resource "kubernetes_persistent_volume_claim_v1" "substrate_valkey"' in terraform
    assert 'storage_class_name = "standard"' in terraform
    assert "prevent_destroy = true" in terraform
    assert 'postrender {' in terraform
    assert "substrate_gke_postrender.py" in terraform
    assert "substrate_crds_hpa_postrender.py" in terraform
    assert "kagent_workerpool_hpa_postrender.py" in terraform
    assert 'resource "helm_release" "recsys_kagent_agent"' not in terraform


def test_runtime_verifier_accepts_keda_static_fallback_defaulting():
    verifier = (ROOT / "jenkins/scripts/test/agentic.sh").read_text(
        encoding="utf-8"
    )
    assert 'fallback.get("behavior", "static") == "static"' in verifier
    assert 'payload["fallback"] ==' not in verifier


def test_a2a_smoke_requires_all_tools_and_a_completed_grounded_answer():
    deploy = (ROOT / "jenkins/scripts/deploy/agentic.sh").read_text(
        encoding="utf-8"
    )
    assert 'status.get("state") != "completed"' in deploy
    assert '"get_user_online_features": (' in deploy
    assert '"get_chunk_by_id": (' in deploy
    assert '"retrieve_rag_context": (' in deploy
    assert '"build_user_rag_context": (' in deploy
    assert "tool_name not in calls or tool_name not in responses" in deploy
    assert 'message.get("role") == "agent"' in deploy
    assert 'answer_messages.append(status["message"])' in deploy
    assert "user feature response does not contain user_id" in deploy
    assert "user feature answer does not state user_id" not in deploy
    assert "collect_chunk_ids(tool_response)" in deploy
    assert "completed without a final text answer" in deploy
    assert "response has no grounded chunk_id" in deploy
    assert "answer does not cite a returned chunk_id" not in deploy
    assert "do not ask questions" not in deploy
    assert "top_k_items=2" in deploy


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


def test_registry_uses_arctl_v04_declarative_resources():
    deploy = (ROOT / "jenkins/scripts/deploy/agentic.sh").read_text(
        encoding="utf-8"
    )
    assert "recsys/recsys-feature-rag-mcp" in deploy
    assert '"apiVersion": "ar.dev/v1alpha1"' in deploy
    assert '"kind": "MCPServer"' in deploy
    assert '"kind": "Agent"' in deploy
    assert 'arctl apply -f "${manifest}"' in deploy
    assert 'arctl get mcp "${registry_name}" --tag "${tag}"' in deploy
    assert 'arctl delete agent "${legacy_name}" --all-tags' in deploy
    assert "recsys/recsys-context-agent-sandbox" in deploy
    assert "legacy_registry_backup" in deploy
    assert "${version/+/-}" in deploy
    context_publish = deploy[deploy.index("publish_context_agent_registry()") :]
    publish_sandbox = context_publish.index(
        'arctl get agent "${registry_name}" --tag "${tag}" -o json'
    )
    backup_legacy = context_publish.index(
        'if agentic_registry_tagged_resource_exists agent "${legacy_name}"'
    )
    delete_legacy = context_publish.index(
        'arctl delete agent "${legacy_name}" --all-tags'
    )
    assert publish_sandbox < backup_legacy < delete_legacy

    ci_values = (ROOT / "infra/helm/recsys-ci/values.yaml").read_text(
        encoding="utf-8"
    )
    assert "arctlVersion: v0.4.0" in ci_values
    assert "e564334357731c59faa3482f2978c21a205a60ad3bcc63a44465607cc74fa343" in ci_values


def test_a2a_smoke_payloads_use_protocol_v03_message_ids():
    deploy = (ROOT / "jenkins/scripts/deploy/agentic.sh").read_text(
        encoding="utf-8"
    )
    autoscale = (
        ROOT / "ops/validation/agentic_context_autoscale.sh"
    ).read_text(encoding="utf-8")
    for script in (deploy, autoscale):
        assert '"messageId": request_id' in script
        assert '"contextId": request_id' in script
        assert '"params": {\n            "id": request_id' not in script
    assert 'a2a_path="api/a2a-sandboxes"' in deploy
    assert 'card_path=".well-known/agent-card.json"' in deploy
    assert 'a2a_path="api/a2a"' not in deploy
