from __future__ import annotations

import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
HELM = ROOT / "infra/helm"
SUSPENDED_CHARTS = (
    "recsys-data-lakehouse",
    "recsys-source-store",
    "recsys-event-stream",
    "recsys-kafka-connect",
    "recsys-streaming",
    "recsys-airflow",
    "recsys-analytics",
    "recsys-demo-web",
)


def render(chart_name: str) -> list[dict]:
    chart = HELM / chart_name
    output = subprocess.check_output(
        [
            "helm",
            "template",
            f"test-{chart_name}",
            str(chart),
            "-f",
            str(chart / "values-compact-12vcpu.yaml"),
        ],
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(output) if isinstance(doc, dict)]


def test_suspended_charts_keep_controllers_and_pvc_contracts() -> None:
    for chart_name in SUSPENDED_CHARTS:
        documents = render(chart_name)
        controllers = [
            doc
            for doc in documents
            if doc.get("kind") in {"Deployment", "StatefulSet"}
        ]
        assert controllers, chart_name
        assert all(doc["spec"]["replicas"] == 0 for doc in controllers), chart_name
        assert not any(doc.get("kind") in {"Job", "Ingress"} for doc in documents)

    stateful_documents = [
        doc
        for chart_name in SUSPENDED_CHARTS
        for doc in render(chart_name)
        if doc.get("kind") == "StatefulSet"
    ]
    assert stateful_documents
    assert all(doc["spec"].get("volumeClaimTemplates") for doc in stateful_documents)


def test_terraform_profile_is_exactly_twelve_vcpu() -> None:
    profile = (ROOT / "infra/terraform/gcp/profiles/compact-12vcpu.tfvars").read_text()
    for contract in (
        'capacity_profile = "compact-12vcpu"',
        'cpu_machine_type = "n2-standard-8"',
        "cpu_min_nodes    = 1",
        "cpu_max_nodes    = 1",
        'ml_machine_type = "e2-standard-4"',
        "ml_min_nodes    = 1",
        "ml_max_nodes    = 1",
        'llm_node_pool_mode = "cpu-services-shared"',
        "enable_gpu_pool    = false",
    ):
        assert contract in profile


def test_retained_autoscalers_are_locked_to_one() -> None:
    charts = (
        "recsys-online-feature-api",
        "recsys-inference-api",
        "recsys-rag-api",
        "recsys-feature-rag-mcp",
        "recsys-recommendation-mcp",
        "recsys-kagent-agent",
        "recsys-recommendation-agent",
        "recsys-coordinator-agent",
    )
    for chart_name in charts:
        values = yaml.safe_load(
            (HELM / chart_name / "values-compact-12vcpu.yaml").read_text()
        )
        assert values["autoscaling"]["minReplicas"] == 1
        assert values["autoscaling"]["maxReplicas"] == 1
