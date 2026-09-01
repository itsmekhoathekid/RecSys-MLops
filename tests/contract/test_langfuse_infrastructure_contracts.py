from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_langfuse_backend_mode_guards_managed_cloud_resources() -> None:
    variables = (ROOT / "infra/terraform/gcp/variables.tf").read_text()
    platform = (
        ROOT / "infra/terraform/gcp/modules/kubernetes-platform/langfuse.tf"
    ).read_text()
    gke = (ROOT / "infra/terraform/gcp/modules/gke/main.tf").read_text()
    network = (ROOT / "infra/terraform/gcp/modules/network/main.tf").read_text()
    services = (
        ROOT / "infra/terraform/gcp/modules/project-services/apis.tf"
    ).read_text()

    assert 'variable "langfuse_backend_mode"' in variables
    assert 'contains(["managed", "in_cluster"], var.langfuse_backend_mode)' in variables
    assert 'default     = "managed"' in variables
    assert "langfuse_managed_backend_deletion_protection" in variables
    assert 'var.config.langfuse_backend_mode == "managed"' in platform
    assert 'count = local.langfuse_managed_backend ? 1 : 0' in platform
    assert (
        'var.config.deploy_langfuse && var.config.langfuse_backend_mode == "managed"'
        in gke
    )
    assert (
        'var.config.deploy_langfuse && var.config.langfuse_backend_mode == "managed"'
        in network
    )
    assert (
        'var.config.deploy_langfuse && var.config.langfuse_backend_mode == "managed"'
        in services
    )
    cloud_sql = platform.split(
        'resource "google_sql_database_instance" "langfuse"', 1
    )[1].split('resource "google_sql_database" "langfuse"', 1)[0]
    memorystore = platform.split(
        'resource "google_redis_instance" "langfuse"', 1
    )[1].split('resource "google_storage_bucket" "langfuse"', 1)[0]
    assert "prevent_destroy" not in cloud_sql
    assert "prevent_destroy" not in memorystore


def test_coursework_values_fit_the_shared_cpu_budget() -> None:
    values = yaml.safe_load(
        (ROOT / "configs/langfuse/values-coursework.yaml").read_text()
    )

    app = values["langfuse"]
    assert app["nodeSelector"] == {"recsys.ai/pool": "cpu-services"}
    assert app["tolerations"] == []
    assert app["extraVolumes"] == []
    assert app["extraVolumeMounts"] == []
    assert all(
        env["name"] != "SHADOW_DATABASE_URL" for env in app["additionalEnv"]
    )
    for component in ("web", "worker"):
        assert app[component]["replicas"] == 1
        assert app[component]["hpa"]["enabled"] is False
        assert app[component]["pdb"]["create"] is False
        assert app[component]["resources"]["requests"] == {
            "cpu": "200m",
            "memory": "512Mi",
        }

    postgres = values["postgresql"]
    assert postgres["deploy"] is True
    assert postgres["args"] == []
    assert postgres["replicaCount"] == 1
    assert postgres["storage"]["requestedSize"] == "10Gi"
    assert postgres["resources"]["requests"] == {
        "cpu": "200m",
        "memory": "512Mi",
    }

    redis = values["redis"]
    assert redis["deploy"] is True
    assert redis["tls"]["enabled"] is False
    assert redis["replica"]["enabled"] is False
    assert redis["dataStorage"]["requestedSize"] == "8Gi"
    assert redis["resources"]["requests"] == {
        "cpu": "100m",
        "memory": "256Mi",
    }

    clickhouse = values["clickhouse"]
    assert clickhouse["cluster"]["replicas"] == 1
    assert clickhouse["cluster"]["storage"]["size"] == "20Gi"
    assert clickhouse["cluster"]["resources"]["requests"] == {
        "cpu": "500m",
        "memory": "2Gi",
    }
    assert clickhouse["keeper"]["enabled"] is True
    assert clickhouse["keeper"]["replicas"] == 1
    assert clickhouse["keeper"]["storage"]["size"] == "5Gi"
    assert clickhouse["keeper"]["resources"]["requests"] == {
        "cpu": "100m",
        "memory": "256Mi",
    }


def test_coursework_profile_preserves_gcs_and_stable_langfuse_keys() -> None:
    platform = (
        ROOT / "infra/terraform/gcp/modules/kubernetes-platform/langfuse.tf"
    ).read_text()
    outputs = (
        ROOT / "infra/terraform/gcp/modules/kubernetes-platform/outputs.tf"
    ).read_text()

    assert 'resource "google_storage_bucket" "langfuse"' in platform
    assert 'count = var.config.deploy_langfuse ? 1 : 0' in platform
    assert '"project-public-key"' in platform
    assert '"project-secret-key"' in platform
    assert 'resource "kubernetes_secret_v1" "langfuse_otel"' in platform
    assert "values-coursework.yaml" in platform
    assert "local.langfuse_in_cluster_backend" in platform
    assert 'output "langfuse_backend_mode"' in outputs
    assert (
        'var.config.deploy_langfuse && var.config.langfuse_backend_mode == "managed"'
        in outputs
    )
