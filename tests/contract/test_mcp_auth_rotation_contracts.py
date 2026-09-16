from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "configs/agentic/mcp-auth-versions.yaml"

MCP_RESOURCE_KINDS = {
    "ConfigMap",
    "Deployment",
    "NetworkPolicy",
    "PodDisruptionBudget",
    "ScaledObject",
    "Service",
    "ServiceAccount",
    "ServiceMonitor",
}

SERVICE_CASES = (
    (
        "featureRag",
        "recsys-feature-rag-mcp",
        "recsys-kagent-agent",
        "recsys-feature-rag-mcp",
        "recsys-context-agent-sandbox",
    ),
    (
        "recommendation",
        "recsys-recommendation-mcp",
        "recsys-recommendation-agent",
        "recsys-recommendation-mcp",
        "recsys-recommendation-agent-sandbox",
    ),
)


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _write_values(tmp_path: Path, name: str, values: dict[str, Any]) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    return path


def _render(
    chart: str,
    values: Path = MANIFEST,
    *extra_args: str,
) -> list[dict[str, Any]]:
    command = [
        "helm",
        "template",
        "contract-test",
        str(ROOT / "infra/helm" / chart),
        "-f",
        str(values),
        *extra_args,
    ]
    output = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [
        document
        for document in yaml.safe_load_all(output)
        if isinstance(document, dict)
    ]


def _render_security(values: Path = MANIFEST) -> list[dict[str, Any]]:
    return _render(
        "recsys-security",
        values,
        "--set",
        "externalSecrets.featureRagMcp.enabled=true",
        "--set",
        "externalSecrets.recommendationMcp.enabled=true",
        "--set",
        "istio.enabled=true",
    )


def _resource(
    documents: list[dict[str, Any]], kind: str, name: str
) -> dict[str, Any]:
    matches = [
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, f"expected one {kind}/{name}, got {len(matches)}"
    return matches[0]


def _mcp_resources(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [document for document in documents if document.get("kind") in MCP_RESOURCE_KINDS]


def _api_principals(documents: list[dict[str, Any]]) -> set[str]:
    policy = _resource(documents, "AuthorizationPolicy", "recsys-api-allow")
    return set(policy["spec"]["rules"][0]["from"][0]["source"]["principals"])


def _assert_agent_binding(
    documents: list[dict[str, Any]],
    *,
    remote_name: str,
    sandbox_name: str,
    revision: str,
    workload_name: str,
    secret_name: str,
    allowed_workloads: set[str],
) -> None:
    remote = _resource(documents, "RemoteMCPServer", remote_name)
    sandbox = _resource(documents, "SandboxAgent", sandbox_name)

    assert remote["metadata"]["annotations"]["recsys.ai/mcp-auth-revision"] == revision
    assert remote["spec"]["url"] == (
        f"http://{workload_name}.kagent.svc.cluster.local:8080/mcp"
    )
    assert remote["spec"]["headersFrom"] == [
        {
            "name": "Authorization",
            "valueFrom": {
                "type": "Secret",
                "name": secret_name,
                "key": "Authorization",
            },
        }
    ]

    assert sandbox["metadata"]["annotations"]["recsys.ai/mcp-auth-revision"] == revision
    assert sandbox["spec"]["declarative"]["deployment"]["env"] == [
        {"name": "RECSYS_MCP_AUTH_REVISION", "value": revision},
        {"name": "RECSYS_AGENT_RELEASE_VERSION", "value": ""},
    ]
    assert set(sandbox["spec"]["sandbox"]["network"]["allowedDomains"]) == {
        f"{workload}.kagent.svc.cluster.local" for workload in allowed_workloads
    }


def test_security_renders_mutable_legacy_and_pinned_immutable_v1() -> None:
    manifest = _manifest()
    documents = _render_security()
    expected_secrets: set[str] = set()
    expected_principals: set[str] = set()

    for service_name, service in manifest["services"].items():
        for revision_name, revision in service["revisions"].items():
            secret_name = revision["secretName"]
            expected_secrets.add(secret_name)
            external_secret = _resource(documents, "ExternalSecret", secret_name)
            spec = external_secret["spec"]

            assert external_secret["metadata"]["namespace"] == "kagent"
            assert external_secret["metadata"]["labels"]["recsys.ai/auth-revision"] == (
                revision_name
            )
            assert external_secret["metadata"]["annotations"] == {
                "recsys.ai/mcp-auth-service": service_name,
                "recsys.ai/mcp-auth-workload": revision["workloadName"],
                "recsys.ai/mcp-auth-deploy": str(revision["deploy"]).lower(),
            }
            assert spec["target"]["name"] == secret_name
            assert spec["target"]["creationPolicy"] == "Owner"
            assert spec["dataFrom"][0]["extract"]["key"] == service["vaultPath"]

            if revision_name == "legacy":
                assert spec["refreshInterval"] == "1h"
                assert "refreshPolicy" not in spec
                assert "immutable" not in spec["target"]
                assert "version" not in spec["dataFrom"][0]["extract"]
            else:
                assert spec["refreshPolicy"] == "CreatedOnce"
                assert "refreshInterval" not in spec
                assert spec["target"]["immutable"] is True
                assert spec["dataFrom"][0]["extract"]["version"] == (
                    revision["vaultVersion"]
                )

            if revision["deploy"]:
                expected_principals.add(
                    f"cluster.local/ns/kagent/sa/{revision['workloadName']}"
                )

    rendered_secrets = {
        document["metadata"]["name"]
        for document in documents
        if document.get("kind") == "ExternalSecret"
        and document.get("metadata", {})
        .get("labels", {})
        .get("recsys.ai/auth-revision")
    }
    assert rendered_secrets == expected_secrets
    assert expected_principals <= _api_principals(documents)


@pytest.mark.parametrize(
    ("service_name", "mcp_chart", "_agent_chart", "_remote_name", "_sandbox_name"),
    SERVICE_CASES,
)
def test_each_deployed_mcp_revision_has_eight_isolated_resources(
    service_name: str,
    mcp_chart: str,
    _agent_chart: str,
    _remote_name: str,
    _sandbox_name: str,
) -> None:
    service = _manifest()["services"][service_name]
    documents = _render(mcp_chart)
    resources = _mcp_resources(documents)
    deployed = {
        revision_name: revision
        for revision_name, revision in service["revisions"].items()
        if revision["deploy"]
    }
    expected_names = {revision["workloadName"] for revision in deployed.values()}

    assert len(resources) == len(deployed) * len(MCP_RESOURCE_KINDS)
    for kind in MCP_RESOURCE_KINDS:
        assert {
            document["metadata"]["name"]
            for document in resources
            if document["kind"] == kind
        } == expected_names

    metric_names: set[str] = set()
    for revision_name, revision in deployed.items():
        workload = revision["workloadName"]
        peer_workloads = expected_names - {workload}
        revision_resources = [
            document
            for document in resources
            if document["metadata"]["name"] == workload
        ]
        assert len(revision_resources) == len(MCP_RESOURCE_KINDS)
        assert all(
            document["metadata"]["labels"]["recsys.ai/auth-revision"]
            == revision_name
            for document in revision_resources
        )

        deployment = _resource(documents, "Deployment", workload)
        pod_template = deployment["spec"]["template"]
        assert deployment["spec"]["selector"]["matchLabels"] == {
            "app.kubernetes.io/name": workload
        }
        assert pod_template["metadata"]["labels"]["app.kubernetes.io/name"] == workload
        assert pod_template["spec"]["serviceAccountName"] == workload
        env_from = pod_template["spec"]["containers"][0]["envFrom"]
        assert env_from == [
            {"configMapRef": {"name": workload}},
            {"secretRef": {"name": revision["secretName"]}},
        ]

        assert _resource(documents, "Service", workload)["spec"]["selector"] == {
            "app.kubernetes.io/name": workload
        }
        assert _resource(documents, "PodDisruptionBudget", workload)["spec"][
            "selector"
        ]["matchLabels"] == {"app.kubernetes.io/name": workload}
        assert _resource(documents, "NetworkPolicy", workload)["spec"][
            "podSelector"
        ]["matchLabels"] == {"app.kubernetes.io/name": workload}
        assert _resource(documents, "ServiceMonitor", workload)["spec"]["selector"][
            "matchLabels"
        ] == {"app.kubernetes.io/name": workload}

        scaled = _resource(documents, "ScaledObject", workload)
        assert scaled["spec"]["scaleTargetRef"] == {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": workload,
        }
        metric = scaled["spec"]["triggers"][0]["metadata"]
        expected_metric = f"{workload.replace('-', '_')}_requests_per_second"
        assert metric["metricName"] == expected_metric
        assert f'pod=~"{workload}-[a-z0-9]+-[a-z0-9]+"' in metric["query"]
        metric_names.add(metric["metricName"])

        allowed_hosts = _resource(documents, "ConfigMap", workload)["data"][
            "MCP_ALLOWED_HOSTS"
        ]
        assert f"{workload}.kagent.svc.cluster.local:8080" in allowed_hosts.split(",")
        for peer in peer_workloads:
            assert f"{peer}.kagent.svc.cluster.local:8080" not in allowed_hosts.split(",")

    assert len(metric_names) == len(deployed)


@pytest.mark.parametrize(
    ("service_name", "_mcp_chart", "agent_chart", "remote_name", "sandbox_name"),
    SERVICE_CASES,
)
def test_agent_cutover_switches_url_and_secret_but_keeps_all_deployed_domains(
    tmp_path: Path,
    service_name: str,
    _mcp_chart: str,
    agent_chart: str,
    remote_name: str,
    sandbox_name: str,
) -> None:
    manifest = _manifest()
    service = manifest["services"][service_name]
    deployed_workloads = {
        revision["workloadName"]
        for revision in service["revisions"].values()
        if revision["deploy"]
    }

    legacy = service["revisions"]["legacy"]
    service["activeRevision"] = "legacy"
    legacy_values = _write_values(tmp_path, f"{service_name}-legacy", manifest)
    legacy_documents = _render(agent_chart, legacy_values)
    _assert_agent_binding(
        legacy_documents,
        remote_name=remote_name,
        sandbox_name=sandbox_name,
        revision="legacy",
        workload_name=legacy["workloadName"],
        secret_name=legacy["secretName"],
        allowed_workloads=deployed_workloads,
    )

    service["activeRevision"] = "v1"
    cutover_values = _write_values(tmp_path, f"{service_name}-cutover", manifest)
    cutover_documents = _render(agent_chart, cutover_values)
    v1 = service["revisions"]["v1"]
    _assert_agent_binding(
        cutover_documents,
        remote_name=remote_name,
        sandbox_name=sandbox_name,
        revision="v1",
        workload_name=v1["workloadName"],
        secret_name=v1["secretName"],
        allowed_workloads=deployed_workloads,
    )

    assert {
        (document["kind"], document["metadata"]["name"])
        for document in legacy_documents
        if document.get("kind") in {"RemoteMCPServer", "SandboxAgent"}
    } == {
        (document["kind"], document["metadata"]["name"])
        for document in cutover_documents
        if document.get("kind") in {"RemoteMCPServer", "SandboxAgent"}
    }


@pytest.mark.parametrize(
    ("service_name", "mcp_chart", "agent_chart", "remote_name", "sandbox_name"),
    SERVICE_CASES,
)
def test_deploy_false_retains_secret_but_removes_workload_domain_and_principal(
    tmp_path: Path,
    service_name: str,
    mcp_chart: str,
    agent_chart: str,
    remote_name: str,
    sandbox_name: str,
) -> None:
    manifest = _manifest()
    service = manifest["services"][service_name]
    service["activeRevision"] = "v1"
    service["revisions"]["legacy"]["deploy"] = False
    values = _write_values(tmp_path, f"{service_name}-retired", manifest)

    legacy = service["revisions"]["legacy"]
    v1 = service["revisions"]["v1"]
    security_documents = _render_security(values)
    assert _resource(security_documents, "ExternalSecret", legacy["secretName"])
    assert _resource(security_documents, "ExternalSecret", v1["secretName"])
    principals = _api_principals(security_documents)
    assert f"cluster.local/ns/kagent/sa/{legacy['workloadName']}" not in principals
    assert f"cluster.local/ns/kagent/sa/{v1['workloadName']}" in principals

    mcp_resources = _mcp_resources(_render(mcp_chart, values))
    assert len(mcp_resources) == len(MCP_RESOURCE_KINDS)
    assert {document["metadata"]["name"] for document in mcp_resources} == {
        v1["workloadName"]
    }

    _assert_agent_binding(
        _render(agent_chart, values),
        remote_name=remote_name,
        sandbox_name=sandbox_name,
        revision="v1",
        workload_name=v1["workloadName"],
        secret_name=v1["secretName"],
        allowed_workloads={v1["workloadName"]},
    )


@pytest.mark.parametrize(
    ("service_name", "mcp_chart", "agent_chart", "remote_name", "sandbox_name"),
    SERVICE_CASES,
)
def test_purge_removes_retired_external_secret_and_keeps_v1_consumers(
    tmp_path: Path,
    service_name: str,
    mcp_chart: str,
    agent_chart: str,
    remote_name: str,
    sandbox_name: str,
) -> None:
    manifest = _manifest()
    service = manifest["services"][service_name]
    service["activeRevision"] = "v1"
    legacy = service["revisions"].pop("legacy")
    v1 = service["revisions"]["v1"]
    values = _write_values(tmp_path, f"{service_name}-purged", manifest)

    security_documents = _render_security(values)
    service_external_secrets = {
        document["metadata"]["name"]
        for document in security_documents
        if document.get("kind") == "ExternalSecret"
        and document.get("metadata", {})
        .get("labels", {})
        .get("recsys.ai/security-scope", "")
        .startswith(service_name)
    }
    assert legacy["secretName"] not in service_external_secrets
    assert service_external_secrets == {v1["secretName"]}

    mcp_resources = _mcp_resources(_render(mcp_chart, values))
    assert len(mcp_resources) == len(MCP_RESOURCE_KINDS)
    assert {document["metadata"]["name"] for document in mcp_resources} == {
        v1["workloadName"]
    }
    _assert_agent_binding(
        _render(agent_chart, values),
        remote_name=remote_name,
        sandbox_name=sandbox_name,
        revision="v1",
        workload_name=v1["workloadName"],
        secret_name=v1["secretName"],
        allowed_workloads={v1["workloadName"]},
    )


def test_coordinator_never_receives_direct_mcp_domains_from_shared_manifest() -> None:
    documents = _render("recsys-coordinator-agent")
    sandbox = _resource(
        documents, "SandboxAgent", "recsys-coordinator-agent-sandbox"
    )
    allowed_domains = sandbox["spec"]["sandbox"]["network"]["allowedDomains"]

    assert allowed_domains == ["kagent-controller.kagent"]
    assert not any("mcp" in domain for domain in allowed_domains)
    assert not any(document.get("kind") == "RemoteMCPServer" for document in documents)
