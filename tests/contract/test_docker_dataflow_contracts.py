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


def render(chart_name: str, *, set_values: tuple[str, ...] = ()) -> str:
    chart = ROOT / "infra/helm" / chart_name
    values = chart / "values-gcp.yaml"
    command = ["helm", "template", "contract", str(chart)]
    if values.is_file():
        command.extend(["-f", str(values)])
    for value in set_values:
        command.extend(["--set", value])
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
    assert not (
        ROOT / "jenkins/scripts/entrypoints/component_build_publish.sh"
    ).exists()
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
            assert key not in owners, (
                f"{key} owned by {owners.get(key)} and {chart_name}"
            )
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


def test_airflow_runtime_is_pinned_to_the_stable_2_9_control_plane():
    dockerfile = (ROOT / "images/data/recsys-airflow/Dockerfile").read_text()
    rendered_airflow = render("recsys-airflow")
    runtime_verifier = (ROOT / "jenkins/scripts/test/runtime.sh").read_text()

    assert "ARG AIRFLOW_VERSION=2.9.3" in dockerfile
    assert "ARG AIRFLOW_PYTHON_VERSION=3.10" in dockerfile
    assert "ARG PYTHON_VERSION" not in dockerfile
    assert "constraints-${AIRFLOW_VERSION}" in dockerfile
    assert "constraints-${AIRFLOW_PYTHON_VERSION}.txt" in dockerfile
    assert "exec airflow webserver" in rendered_airflow
    assert "exec airflow scheduler" in rendered_airflow
    assert "airflow api-server" not in rendered_airflow
    assert "airflow dag-processor" not in rendered_airflow
    assert rendered_airflow.count("name: AIRFLOW__CORE__EXECUTOR") == 2
    assert "component_test_airflow_dag_registered" in runtime_verifier
    assert "airflow dags list --output plain" in runtime_verifier
    assert "airflow dags trigger" not in runtime_verifier
    assert "airflow dags list-runs" not in runtime_verifier
    assert "airflow dags state" not in runtime_verifier

    data_config = render("recsys-data-config")
    for pipeline in ("DP1", "DP2", "DP3"):
        assert f"{pipeline}_DAG_SCHEDULE" in data_config
    assert "DATA_PLATFORM_DAG_SCHEDULE" not in data_config
    assert "BATCH_FEATURE_DAG_SCHEDULE" not in data_config


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


def test_feature_store_image_matches_feast_sqlalchemy_registry_driver():
    dockerfile = (
        ROOT / "images/data/recsys-feature-store/Dockerfile"
    ).read_text()
    registry = (
        ROOT
        / "apps/data-platform/src/feature_store/sql_registry_state.py"
    ).read_text()
    serving_project = (ROOT / "apps/api-serving/pyproject.toml").read_text()
    assert "psycopg2-binary" in dockerfile
    assert "psycopg2-binary" in serving_project
    assert 'drivername="postgresql+psycopg2"' in registry
    assert 'drivername="postgresql+psycopg"' not in registry


def test_stream_verifier_is_read_only_and_checks_deployed_services():
    verifier = (ROOT / "jenkins/scripts/test/data_platform.sh").read_text()
    assert "test_debezium_connector_tasks" in verifier
    assert "realtime-flink-offline-store" in verifier
    assert "realtime-flink-online-store" in verifier
    assert "redis-cli PING" in verifier
    assert "pg_isready" in verifier
    assert "kind: Job" not in verifier
    assert "--verification-event-id" not in verifier
    assert "redis-cli DEL" not in verifier


def test_spark_can_reach_feature_stores_without_weakening_namespace_mtls():
    rendered = render(
        "recsys-security",
        set_values=("istio.namespaces[5]=recsys-dataflow",),
    )
    resources = {
        (document["kind"], document["metadata"]["name"]): document
        for document in yaml.safe_load_all(rendered)
        if isinstance(document, dict) and document.get("kind")
    }

    strict = resources[("PeerAuthentication", "recsys-strict-mtls")]
    assert strict["spec"]["mtls"]["mode"] == "STRICT"

    stores = {"feature-postgres": 5432, "redis": 6379}
    allowed_namespaces = {
        "recsys-dataflow",
        "api-serving",
        "kubeflow",
        "datahub",
        "observability",
    }
    for store, port in stores.items():
        peer_auth = resources[
            ("PeerAuthentication", f"recsys-dataflow-{store}-permissive")
        ]
        assert peer_auth["spec"]["selector"]["matchLabels"]["app"] == store
        assert peer_auth["spec"]["portLevelMtls"][port]["mode"] == "PERMISSIVE"
        assert "mtls" not in peer_auth["spec"]

        authorization = resources[
            ("AuthorizationPolicy", f"recsys-dataflow-{store}-allow")
        ]
        assert authorization["spec"]["selector"]["matchLabels"]["app"] == store
        assert authorization["spec"]["rules"][0]["to"][0]["operation"][
            "ports"
        ] == [str(port)]

        network_policy = resources[
            ("NetworkPolicy", f"recsys-dataflow-{store}-ingress")
        ]
        ingress = network_policy["spec"]["ingress"][0]
        assert {item["namespaceSelector"]["matchLabels"][
            "kubernetes.io/metadata.name"
        ] for item in ingress["from"]} == allowed_namespaces
        assert ingress["ports"] == [{"protocol": "TCP", "port": port}]


def test_flink_taskmanagers_fit_the_production_node_pool_cpu_budget():
    rendered = render("recsys-streaming")
    resources = {
        (document["kind"], document["metadata"]["name"]): document
        for document in yaml.safe_load_all(rendered)
        if isinstance(document, dict) and document.get("kind")
    }
    taskmanager = resources[("Deployment", "flink-taskmanager")]
    container = taskmanager["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"]["cpu"] == "300m"


def test_release_planner_and_jenkins_use_one_global_plan():
    jenkinsfile = (ROOT / "Jenkinsfile").read_text()
    groovy = (ROOT / "jenkins/pipeline/component_pipeline.groovy").read_text()
    assert "--plan-output .ci-release-plan.json" in jenkinsfile
    assert "release_build_publish.sh .ci-release-plan.json" in jenkinsfile
    assert "deployReleasePlan" in jenkinsfile
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
