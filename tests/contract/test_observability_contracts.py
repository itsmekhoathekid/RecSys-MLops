from __future__ import annotations

import shutil
import subprocess
import json
from pathlib import Path

import pytest
import yaml


def _documents(rendered: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]


def _render_observability(*extra_args: str) -> list[dict]:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "recsys-observability",
            "infra/helm/recsys-observability",
            "--namespace",
            "observability",
            *extra_args,
        ],
        text=True,
    )
    return _documents(rendered)


def _render_data_platform(*extra_args: str) -> list[dict]:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "recsys-data-platform",
            "infra/helm/recsys-data-platform",
            "--namespace",
            "recsys-dataflow",
            *extra_args,
        ],
        text=True,
    )
    return _documents(rendered)


def _by_kind_name(docs: list[dict]) -> dict[tuple[str, str], dict]:
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}


def test_grafana_is_configured_for_public_gateway_origin():
    docs = _render_observability()
    deployment = _by_kind_name(docs)[("Deployment", "recsys-grafana")]
    env = {
        item["name"]: item["value"]
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert env["GF_SERVER_DOMAIN"] == "grafana.recsys.local"
    assert env["GF_SERVER_ROOT_URL"] == "http://grafana.recsys.local/"


def test_ab_dashboard_contains_shadow_candidate_proof_panels():
    dashboard = json.loads(
        Path("infra/helm/recsys-observability/dashboards/model-ab-testing.json").read_text(encoding="utf-8")
    )
    panels = {panel["title"]: panel for panel in dashboard["panels"]}

    assert "Shadow Candidate Before A/B" in panels
    assert "Shadow Inference Count by Status" in panels
    assert "Shadow Candidate P95 Latency" in panels
    expressions = "\n".join(
        target.get("expr", "")
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    )
    assert "recsys_api_shadow_inferences_total" in expressions
    assert "recsys_api_shadow_latency_seconds_bucket" in expressions
    assert "recsys_api_shadow_score_mean" in expressions

    experiment = next(item for item in dashboard["templating"]["list"] if item["name"] == "experiment")
    assert "recsys_api_rollout_config_info" in experiment["query"]
    rollout_panel = panels["Active Rollout Configuration (Always On)"]
    assert rollout_panel["targets"][0]["instant"] is True
    assert "recsys_api_rollout_config_info" in rollout_panel["targets"][0]["expr"]

    for title in [
        "Prediction Rate",
        "Candidate Share",
        "P95 Latency Delta",
        "Avg Confidence",
        "Shadow Inference Count by Status",
        "Shadow Candidate P95 Latency",
    ]:
        assert "or vector(0)" in panels[title]["targets"][0]["expr"]


def test_prometheus_scrapes_api_metrics_once_per_pod():
    docs = _render_observability()
    resources = _by_kind_name(docs)
    config = resources[("ConfigMap", "recsys-prometheus-config")]["data"]["prometheus.yml"]

    assert "job_name: recsys-kubernetes-pods" in config
    assert "prometheus_io_scrape" in config
    assert "job_name: recsys-api-serving" not in config
    assert "job_name: recsys-online-feature-api" not in config

    deployment = resources[("Deployment", "recsys-prometheus")]
    assert "checksum/prometheus-config" in deployment["spec"]["template"]["metadata"]["annotations"]
    data_volume = next(
        volume for volume in deployment["spec"]["template"]["spec"]["volumes"] if volume["name"] == "data"
    )
    assert data_volume["emptyDir"] == {}
    assert deployment["spec"]["template"]["spec"]["securityContext"]["fsGroup"] == 65534
    assert ("PersistentVolumeClaim", "recsys-prometheus-data") not in resources

    persistent_docs = _render_observability("--set", "persistence.prometheus.enabled=true")
    persistent_resources = _by_kind_name(persistent_docs)
    persistent_deployment = persistent_resources[("Deployment", "recsys-prometheus")]
    persistent_data = next(
        volume
        for volume in persistent_deployment["spec"]["template"]["spec"]["volumes"]
        if volume["name"] == "data"
    )
    assert persistent_data["persistentVolumeClaim"]["claimName"] == "recsys-prometheus-data"
    pvc = persistent_resources[("PersistentVolumeClaim", "recsys-prometheus-data")]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "10Gi"


def test_flink_exports_live_prometheus_metrics_from_job_and_task_managers():
    docs = _render_data_platform()
    resources = _by_kind_name(docs)

    for deployment_name in ("flink-jobmanager", "flink-taskmanager"):
        pod = resources[("Deployment", deployment_name)]["spec"]["template"]
        annotations = pod["metadata"]["annotations"]
        container = pod["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}

        assert annotations["prometheus.io/scrape"] == "true"
        assert annotations["prometheus.io/path"] == "/metrics"
        assert annotations["prometheus.io/port"] == "9249"
        assert any(port.get("name") == "metrics" and port["containerPort"] == 9249 for port in container["ports"])
        assert "org.apache.flink.metrics.prometheus.PrometheusReporterFactory" in env["FLINK_PROPERTIES"]

    dockerfile = Path("apps/data-platform/Dockerfile.flink").read_text(encoding="utf-8")
    assert "flink-metrics-prometheus-1.19.3.jar" in dockerfile
    assert "/opt/flink/plugins/prometheus" in dockerfile


def test_flink_taskmanager_health_requires_jobmanager_registration():
    docs = _render_data_platform()
    deployment = _by_kind_name(docs)[("Deployment", "flink-taskmanager")]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}

    assert env["POD_IP"]["valueFrom"]["fieldRef"]["fieldPath"] == "status.podIP"
    for probe_name in ("readinessProbe", "livenessProbe"):
        command = container[probe_name]["exec"]["command"]
        assert "http://flink-jobmanager:8081/taskmanagers" in command[-1]
        assert '${POD_IP}:6122-' in command[-1]

    assert container["livenessProbe"]["failureThreshold"] == 6


def test_data_pipeline_dashboard_uses_live_metrics_and_explicit_freshness():
    dashboard = json.loads(
        Path("infra/helm/recsys-observability/dashboards/data-pipeline-observability.json").read_text(
            encoding="utf-8"
        )
    )
    panels = {panel["title"]: panel for panel in dashboard["panels"]}
    expressions = "\n".join(
        target.get("expr", "")
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    )

    assert "Event Throughput" in panels
    assert "Stream Freshness" in panels
    assert "Redis Connected Clients" in panels
    throughput_expression = panels["Event Throughput"]["targets"][0]["expr"]
    assert "flink_taskmanager_job_task_numRecordsOutPerSecond" in throughput_expression
    assert 'namespace="recsys-dataflow"' in throughput_expression
    assert 'job_name=~".*realtime_online$"' in throughput_expression
    assert 'task_name=~"Source:_cdc_behavior_events_source.*"' in throughput_expression
    assert "recsys_streaming_events_total" not in throughput_expression
    assert panels["Event Throughput"]["fieldConfig"]["defaults"]["decimals"] >= 2
    assert "recsys_streaming_events_total" in expressions
    assert "pipeline_role=\"online\"" in expressions
    assert "recsys_ml_feature_drift_psi" in expressions
    assert "redis_commands_processed_total" in expressions
    assert "redis_connected_clients" in expressions
    assert "push_time_seconds" in expressions
    assert "recsys_streaming_event_count" not in expressions
    assert "recsys_feature_drift_score" not in expressions
    assert "window_start" not in expressions


def test_gcp_observability_values_do_not_take_ownership_of_existing_namespace():
    values = yaml.safe_load(
        Path("infra/helm/recsys-observability/values-gcp.yaml").read_text(encoding="utf-8")
    )

    assert values["namespace"] == {"create": False, "name": "observability"}


def test_observability_does_not_scrape_missing_sql_exporter():
    docs = _render_observability()
    prometheus = _by_kind_name(docs)[("ConfigMap", "recsys-prometheus-config")]

    assert "recsys-monitoring-sql-exporter" not in prometheus["data"]["prometheus.yml"]
