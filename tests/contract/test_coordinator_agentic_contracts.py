from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from jenkins.python.agent_registry_release import build_resource

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/recsys-coordinator-agent"
CONTRACT = ROOT / "configs/agentic/recsys-coordinator-agent/tools-contract.json"


def _agentic_deploy_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "jenkins/scripts/deploy/agentic.sh",
            ROOT / "jenkins/scripts/lib/runtime.sh",
            *sorted((ROOT / "jenkins/scripts/deploy/agentic").glob("*.sh")),
        )
    )


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


def test_coordinator_sandbox_references_only_two_a2a_agents() -> None:
    documents = _render()
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert sum(item.get("kind") == "SandboxAgent" for item in documents) == 1
    assert not any(item.get("kind") == "Agent" for item in documents)
    assert sum(item.get("kind") == "ScaledObject" for item in documents) == 1
    assert sum(item.get("kind") == "PodDisruptionBudget" for item in documents) == 1
    assert not any(item.get("kind") == "RemoteMCPServer" for item in documents)

    agent = _resource(documents, "SandboxAgent", "recsys-coordinator-agent-sandbox")
    assert agent["apiVersion"] == "kagent.dev/v1alpha2"
    assert agent["spec"]["declarative"]["runtime"] == "python"
    assert "platform" not in agent["spec"]
    assert agent["spec"]["substrate"]["workerPoolRef"]["name"] == (
        "recsys-coordinator-sandbox-pool"
    )
    tools = agent["spec"]["declarative"]["tools"]
    assert all("isolateSessions" not in item for item in tools)
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
    assert mcp_tools == []


def test_coordinator_prompt_locks_routing_grounding_and_partial_results() -> None:
    agent = _resource(_render(), "SandboxAgent", "recsys-coordinator-agent-sandbox")
    prompt = agent["spec"]["declarative"]["systemMessage"]
    for requirement in (
        "exactly two available A2A specialist tools",
        "You have no MCP tools",
        "call ask_user",
        "kagent__NS__recsys_context_agent_sandbox",
        "kagent__NS__recsys_recommendation_agent_sandbox",
        "null is not an empty array",
        "Never rerank",
        "chunk_id",
        "Never invent data",
        "Recommendation exactly once and then Context",
        "response is still terminal",
    ):
        assert requirement in prompt
    assert "builtin/a2a-communication" not in prompt
    assert "consider retrying" not in prompt.lower()
    assert "recsys.ai/model-config-revision" not in agent["metadata"].get(
        "annotations", {}
    )
    assert "Runtime model configuration revision:" not in prompt
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
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": "recsys-coordinator-sandbox-pool-deployment",
    }
    assert (spec["minReplicaCount"], spec["maxReplicaCount"]) == (1, 1)
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
    terraform = (
        ROOT / "infra/terraform/gcp/modules/kubernetes-platform/kagent.tf"
    ).read_text(encoding="utf-8")
    assert (
        'resource "kubernetes_manifest" "recsys_coordinator_sandbox_pool"' in terraform
    )
    assert 'name      = "recsys-coordinator-sandbox-pool"' in terraform
    assert 'computed_fields = ["spec.replicas"]' in terraform
    assert '"ate.dev/worker-pool"' in terraform
    assert "scaleSelector" not in terraform


def test_registry_manifest_records_exact_coordinator_dependencies() -> None:
    commit = "0123456789abcdef0123456789abcdef01234567"
    manifest = build_resource(
        "coordinator-agent",
        commit=commit,
        git_url="https://example.invalid/recsys.git",
        chart_reference=(
            "oci://asia-southeast1-docker.pkg.dev/project/recsys/helm/"
            "recsys-coordinator-agent@sha256:" + "a" * 64
        ),
    )
    assert manifest["metadata"]["name"] == "recsys-coordinator-agent-sandbox"
    assert manifest["metadata"]["labels"]["recsys.dev/variant"] == "sandbox"
    assert manifest["metadata"]["annotations"]["recsys.dev/dependencies"].split(
        ","
    ) == [
        "recsys/recsys-feature-rag-mcp@0.2.0-g0123456789ab",
        "recsys/recsys-recommendation-mcp@0.2.0-g0123456789ab",
        "recsys/recsys-context-agent-sandbox@0.2.0-g0123456789ab",
        "recsys/recsys-recommendation-agent-sandbox@0.2.0-g0123456789ab",
    ]
    assert len(manifest["spec"]["mcpServers"]) == 2


def test_coordinator_ci_and_deploy_dependencies_are_wired() -> None:
    deploy_script = _agentic_deploy_source()
    assert "Pass the Recommendation Agent exactly this complete JSON request" in (
        deploy_script
    )
    assert 'candidate_item_ids\\":null,\\"top_k\\":1' in deploy_script
    assert 'candidate_item_ids\\":[800078,800079]' in deploy_script
    assert deploy_script.count("top_k=1") >= 2
    assert deploy_script.count("top_k_items=1") >= 2
    assert "COORDINATOR_A2A_REQUEST_TIMEOUT_SECONDS:-600" in deploy_script
    assert "COORDINATOR_A2A_MAX_ATTEMPTS:-1" in deploy_script
    assert "COORDINATOR_A2A_ADMISSION_MAX_ATTEMPTS:-6" in deploy_script
    assert '"http_422"' in deploy_script
    assert "assert_usable_agent_response" in deploy_script
    assert (
        "COORDINATOR_SMOKE_CASES:-context_agent,context_chunk_agent,"
        "context_user_rag_agent,"
        "recommendation_agent,recommendation_candidates_agent,composite_agents"
        in deploy_script
    )
    coordinator_smoke = (
        (ROOT / "jenkins/scripts/deploy/agentic/a2a.sh")
        .read_text(encoding="utf-8")
        .split("coordinator_a2a_smoke()", 1)[1]
        .split("agentic_a2a_smoke()", 1)[0]
    )
    assert 'for attempt in $(seq 1 "${max_attempts}")' in coordinator_smoke
    assert "for admission_attempt in range(1, admission_attempts + 1)" in (
        coordinator_smoke
    )
    assert '"no free workers"' in coordinator_smoke
    assert "admission_attempt == admission_attempts" in coordinator_smoke
    assert 'evidence[case_name].append(body)' in coordinator_smoke
    assert 'call_args.append(data.get("args", {}))' in coordinator_smoke
    assert 'metadata.get("adk_type") or metadata.get("kagent_type")' in (
        coordinator_smoke
    )
    assert "def specialist_payload(index):" in coordinator_smoke
    assert '"candidate_item_ids": [800078, 800079]' in coordinator_smoke
    assert '"top_k": 2' in coordinator_smoke
    assert "invalid post-tool clarification" in coordinator_smoke
    assert "json.loads(specialist_request(0))" in coordinator_smoke
    assert "for attempt in 1 2 3" not in coordinator_smoke
    components = json.loads(
        (ROOT / "jenkins/config/components.json").read_text(encoding="utf-8")
    )["components"]
    coordinator = next(
        item for item in components if item["name"] == "coordinator_agent"
    )
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
        "global-model-config",
        "context-agent",
        "recommendation-agent",
        "coordinator-agent-registry",
    ]
    assert units["coordinator-agent-registry"]["dependsOn"] == [
        "feature-rag-mcp-registry",
        "recommendation-mcp-registry",
        "context-agent-registry",
        "recommendation-agent-registry",
    ]


def test_coordinator_shell_entrypoints_are_syntactically_valid() -> None:
    for relative_path in (
        "jenkins/scripts/ci/agentic.sh",
        "jenkins/scripts/deploy/agentic.sh",
        "jenkins/scripts/deploy/agentic/a2a.sh",
        "jenkins/scripts/deploy/agentic/kubernetes.sh",
        "jenkins/scripts/deploy/agentic/mcp.sh",
        "jenkins/scripts/deploy/agentic/registry.sh",
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
            "jenkins/scripts/deploy/agentic/a2a.sh",
            "ops/validation/coordinator_agentic_autoscale.sh",
            "ops/validation/agentic_autoscale_capture.sh",
        )
    )
    assert '"role": "ROLE_USER"' in a2a_generators
    assert '"role": "user"' not in a2a_generators
