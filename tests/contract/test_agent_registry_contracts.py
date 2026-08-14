from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_agent_registry_uses_pinned_oci_chart_and_external_pgvector() -> None:
    terraform = (ROOT / "infra/terraform/gcp/agent_registry.tf").read_text()
    values = (ROOT / "configs/agentregistry/values.yaml").read_text()
    postgres = (
        ROOT
        / "infra/helm/recsys-agent-registry-postgres/templates/statefulset.yaml"
    ).read_text()
    postgres_values = (
        ROOT / "infra/helm/recsys-agent-registry-postgres/values.yaml"
    ).read_text()

    assert 'default     = "0.4.0"' in terraform
    assert "oci://ghcr.io/agentregistry-dev/agentregistry/charts" in terraform
    assert 'resource "helm_release" "agentregistry_postgres"' in terraform
    assert "type: external" in values
    assert "name: agentregistry-runtime" in values
    assert "AGENT_REGISTRY_DATABASE_URL" in values
    assert "0.8.6-pg16" in postgres_values
    assert "kind: StatefulSet" in postgres
    assert "POSTGRES_PASSWORD" in postgres


def test_agent_registry_secret_is_vault_backed_and_namespace_scoped() -> None:
    security_values = (ROOT / "infra/helm/recsys-security/values.yaml").read_text()
    bootstrap = (ROOT / "ops/gcp/bootstrap_vault.sh").read_text()
    values = (ROOT / "configs/agentregistry/values.yaml").read_text()

    assert "secretName: agentregistry-runtime" in security_values
    assert "vaultPath: agentregistry" in security_values
    assert "AGENT_REGISTRY_DATABASE_URL" in bootstrap
    assert "watchedNamespaces:" in values
    assert "- agentregistry" in values
    assert "- kagent" in values


def test_agent_registry_operations_files_are_valid() -> None:
    for relative in (
        "ops/gcp/bootstrap_vault.sh",
        "ops/validation/agent_registry_smoke.sh",
    ):
        subprocess.run(
            ["bash", "-n", str(ROOT / relative)],
            cwd=ROOT,
            check=True,
        )
