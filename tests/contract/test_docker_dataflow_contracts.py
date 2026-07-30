from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DATA_CHARTS = (
    "recsys-data-config",
    "recsys-data-lakehouse",
    "recsys-source-store",
    "recsys-event-stream",
    "recsys-kafka-connect",
    "recsys-feature-store",
    "recsys-streaming",
    "recsys-airflow",
)


def render(chart_name: str) -> str:
    chart = ROOT / "infra/helm" / chart_name
    values = chart / "values-gcp.yaml"
    command = ["helm", "template", "contract", str(chart)]
    if values.is_file():
        command.extend(["-f", str(values)])
    return subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_image_catalog_has_fifteen_images_and_one_spark():
    catalog = json.loads((ROOT / "images/catalog.json").read_text())
    assert len(catalog["images"]) == 15
    assert {name for name in catalog["images"] if name.endswith("-spark")} == {
        "recsys-spark"
    }


def test_monolithic_data_platform_chart_and_component_dispatchers_are_deleted():
    assert not (ROOT / "infra/helm/recsys-data-platform").exists()
    assert not (ROOT / "jenkins/scripts/entrypoints/component_deploy.sh").exists()
    assert not (ROOT / "jenkins/scripts/entrypoints/component_build_publish.sh").exists()
    assert not (ROOT / "jenkins/scripts/deploy/data_platform.sh").exists()
    assert not (ROOT / "jenkins/scripts/deploy/dispatch.sh").exists()
    assert not (ROOT / "jenkins/scripts/build/dispatch.sh").exists()


def test_all_split_data_charts_render():
    for chart_name in DATA_CHARTS:
        rendered = render(chart_name)
        assert "registry.example.invalid" not in rendered or chart_name in {
            "recsys-data-config",
            "recsys-source-store",
            "recsys-kafka-connect",
            "recsys-feature-store",
            "recsys-streaming",
            "recsys-airflow",
        }


def test_split_charts_have_unique_kubernetes_resource_owners():
    owners: dict[tuple[str, str, str], str] = {}
    for chart_name in DATA_CHARTS:
        for document in yaml.safe_load_all(render(chart_name)):
            if not isinstance(document, dict) or not document.get("kind"):
                continue
            metadata = document.get("metadata", {})
            key = (
                str(document["kind"]),
                str(metadata.get("namespace", "recsys-dataflow")),
                str(metadata.get("name")),
            )
            assert key not in owners, f"{key} owned by {owners.get(key)} and {chart_name}"
            owners[key] = chart_name


def test_resource_ownership_matches_release_boundaries():
    rendered = {name: render(name) for name in DATA_CHARTS}
    assert "name: data-platform-minio" in rendered["recsys-data-lakehouse"]
    assert "name: source-postgres" in rendered["recsys-source-store"]
    assert "name: kafka" in rendered["recsys-event-stream"]
    assert "name: kafka-connect" in rendered["recsys-kafka-connect"]
    assert "name: feature-postgres" in rendered["recsys-feature-store"]
    assert "name: redis" in rendered["recsys-feature-store"]
    assert "name: flink-jobmanager" in rendered["recsys-streaming"]
    assert "name: airflow-scheduler" in rendered["recsys-airflow"]


def test_unified_spark_and_dp_profiles_are_the_only_batch_contract():
    assert (ROOT / "configs/data-platform/spark/dp1.yaml").is_file()
    assert (ROOT / "configs/data-platform/spark/dp2.yaml").is_file()
    assert (ROOT / "configs/data-platform/spark/dp3.yaml").is_file()
    assert not list((ROOT / "configs/data-platform/spark").glob("batch*.yaml"))
    dag = (
        ROOT
        / "apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py"
    ).read_text()
    assert "DP3_CONFIG" in dag
    assert "SPARK_BATCH" not in dag
    assert "dp3_offline_feature_entrypoint.py" in dag


def test_unified_spark_contains_all_three_domain_capabilities():
    dockerfile = (ROOT / "images/data/recsys-spark/Dockerfile").read_text()
    smoke = (ROOT / "jenkins/scripts/test/unified_spark_image.sh").read_text()
    for artifact in (
        "iceberg-spark-runtime-3.5_2.12",
        "hudi-spark3.5-bundle_2.12",
        "hadoop-aws",
        "aws-java-sdk-bundle",
        "postgresql-${POSTGRES_JDBC_VERSION}.jar",
    ):
        assert artifact in dockerfile
    for source in (
        "apps/data-platform/src",
        "apps/data-platform/data-generator",
        "apps/data-platform/feature-store",
        "apps/ml-system/src",
        "apps/analytics/src",
    ):
        assert f"COPY {source} " in dockerfile
    assert "PYSPARK_PYTHON=/opt/venv/bin/python" in dockerfile
    assert "PYSPARK_DRIVER_PYTHON=/opt/venv/bin/python" in dockerfile
    assert "generator_config" in smoke
    assert "cli.prepare_bst_training_data" in smoke
    assert "sync_silver" in smoke
    assert "postgresql-42.7.7.jar" in smoke


def test_release_planner_and_jenkins_use_one_global_plan():
    jenkinsfile = (ROOT / "Jenkinsfile").read_text()
    groovy = (ROOT / "jenkins/pipeline/component_pipeline.groovy").read_text()
    assert "--plan-output .ci-release-plan.json" in jenkinsfile
    assert "release_build_publish.sh .ci-release-plan.json" in jenkinsfile
    assert "runReleaseDeployPlan" in jenkinsfile
    assert "runComponentDeployBranches" not in groovy


def test_terraform_bootstraps_split_releases_but_jenkins_owns_runtime_updates():
    terraform = (ROOT / "infra/terraform/gcp/recsys_services.tf").read_text()
    for resource in (
        "recsys_data_config",
        "recsys_data_lakehouse",
        "recsys_source_store",
        "recsys_event_stream",
        "recsys_kafka_connect",
        "recsys_feature_store",
        "recsys_streaming",
        "recsys_airflow",
    ):
        assert f'resource "helm_release" "{resource}"' in terraform
    assert 'resource "helm_release" "recsys_data_platform"' not in terraform
    assert terraform.count("ignore_changes = all") >= 10
    locals_source = (ROOT / "infra/terraform/gcp/locals.tf").read_text()
    assert '"secret.create"        = "false"' in locals_source
    assert '"kserve.secret.create"                         = "false"' in locals_source
