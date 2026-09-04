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


def test_image_catalog_has_required_split_runtime_images():
    catalog = json.loads((ROOT / "images/catalog.json").read_text())
    assert "recsys-feature-rag-mcp" in catalog["images"]
    assert {
        "recsys-ingestion",
        "recsys-kafka-connect-admin",
        "recsys-datahub-ops",
        "recsys-rag-model-e5",
        "recsys-rag-indexer",
        "recsys-rag-admin",
        "recsys-spark-runtime",
        "recsys-spark-data",
        "recsys-spark-analytics",
        "recsys-spark-ml",
    } <= catalog["images"].keys()
    assert "recsys-data-ingestion" not in catalog["images"]
    assert "recsys-spark" not in catalog["images"]
    assert not (ROOT / "images/data/recsys-data-ingestion/Dockerfile").exists()
    assert not (ROOT / "images/data/recsys-spark/Dockerfile").exists()


def test_split_data_images_keep_domain_dependencies_bounded():
    ingestion = (ROOT / "images/data/recsys-ingestion/Dockerfile").read_text()
    kafka_admin = (
        ROOT / "images/data/recsys-kafka-connect-admin/Dockerfile"
    ).read_text()
    datahub_ops = (ROOT / "images/data/recsys-datahub-ops/Dockerfile").read_text()
    rag_indexer = (ROOT / "images/data/recsys-rag-indexer/Dockerfile").read_text()
    rag_admin = (ROOT / "images/data/recsys-rag-admin/Dockerfile").read_text()
    airflow = (ROOT / "images/data/recsys-airflow/Dockerfile").read_text()

    for dependency in (
        "acryl-datahub",
        "feast",
        "onnxruntime",
        "pymilvus",
        "transformers",
    ):
        assert dependency not in ingestion
    for excluded_source in ("src/metadata", "src/rag_data", "data-generator"):
        assert excluded_source not in kafka_admin
    for excluded_source in ("src/rag_data", "data-generator", "rag-runtime"):
        assert excluded_source not in datahub_ops
    for excluded_dependency in ("acryl-datahub", "apache-airflow"):
        assert excluded_dependency not in rag_indexer
    for excluded_source in ("src/rag_data", "src/metadata", "data-generator"):
        assert excluded_source not in rag_admin
    assert "psycopg2-binary==2.9.12" in rag_admin
    assert "uv pip check" in rag_admin
    for excluded_source in ("data-generator", "feature-store/rag_feature_repo"):
        assert excluded_source not in airflow


def test_rag_admin_image_smoke_covers_the_feast_sql_registry_driver():
    engine = (ROOT / "jenkins/scripts/build/engine.sh").read_text()
    smoke = (ROOT / "jenkins/scripts/test/rag_admin_image.sh").read_text()

    assert '"${name}" == "recsys-rag-admin"' in engine
    assert "rag_admin_image.sh" in engine
    assert "import psycopg2" in smoke
    assert "from feast.infra.registry.sql import SqlRegistry" in smoke


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


def test_event_stream_persists_kafka_and_zookeeper_state():
    documents = list(yaml.safe_load_all(render("recsys-event-stream")))
    by_kind_name = {
        (document["kind"], document["metadata"]["name"]): document
        for document in documents
        if isinstance(document, dict) and document.get("kind")
    }

    kafka_pvc = by_kind_name[("PersistentVolumeClaim", "kafka-data")]
    kafka = by_kind_name[("Deployment", "kafka")]
    zookeeper_pvc = by_kind_name[("PersistentVolumeClaim", "zookeeper-data")]
    assert kafka_pvc["spec"]["storageClassName"] == "standard-rwo"
    assert zookeeper_pvc["spec"]["storageClassName"] == "standard-rwo"
    kafka_env = {
        item["name"]: item["value"]
        for item in kafka["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert kafka_pvc["spec"]["resources"]["requests"]["storage"] == "60Gi"
    assert kafka_env["KAFKA_LOG_RETENTION_HOURS"] == "24"
    assert kafka_env["KAFKA_LOG_SEGMENT_BYTES"] == "268435456"
    assert kafka_env["KAFKA_LOG_RETENTION_CHECK_INTERVAL_MS"] == "60000"
    assert zookeeper_pvc["spec"]["resources"]["requests"]["storage"] == "5Gi"

    kafka = by_kind_name[("Deployment", "kafka")]
    zookeeper = by_kind_name[("Deployment", "zookeeper")]
    assert kafka["spec"]["strategy"]["type"] == "Recreate"
    assert zookeeper["spec"]["strategy"]["type"] == "Recreate"
    assert kafka["spec"]["template"]["spec"]["securityContext"] == {
        "fsGroup": 1000,
        "fsGroupChangePolicy": "OnRootMismatch",
    }
    kafka_env = {
        entry["name"]: entry["value"]
        for entry in kafka["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert kafka_env["KAFKA_LOG_DIRS"] == "/var/lib/kafka/data/kafka"
    assert kafka["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"][
        "claimName"
    ] == "kafka-data"
    assert zookeeper["spec"]["template"]["spec"]["volumes"][0][
        "persistentVolumeClaim"
    ]["claimName"] == "zookeeper-data"


def test_kafka_connect_internal_topics_match_small_cluster_partition_count():
    documents = list(yaml.safe_load_all(render("recsys-kafka-connect")))
    deployment = next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document["metadata"]["name"] == "kafka-connect"
    )
    env = {
        entry["name"]: entry["value"]
        for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["CONNECT_OFFSET_STORAGE_PARTITIONS"] == "4"
    assert env["CONNECT_STATUS_STORAGE_PARTITIONS"] == "4"


def test_source_schema_hook_can_use_spare_gcp_ml_node_capacity():
    documents = list(yaml.safe_load_all(render("recsys-source-store")))
    schema_job = next(
        document
        for document in documents
        if document.get("kind") == "Job"
        and document["metadata"]["name"] == "init-source-schema"
    )
    assert schema_job["spec"]["template"]["spec"]["tolerations"] == [
        {
            "key": "recsys.ai/workload",
            "operator": "Equal",
            "value": "ml-system",
            "effect": "NoSchedule",
        }
    ]


def test_cdc_registration_hook_can_use_spare_gcp_ml_node_capacity():
    documents = list(yaml.safe_load_all(render("recsys-kafka-connect")))
    registration_job = next(
        document
        for document in documents
        if document.get("kind") == "Job"
        and document["metadata"]["name"] == "register-realtime-cdc-connector"
    )
    assert registration_job["spec"]["template"]["spec"]["tolerations"] == [
        {
            "key": "recsys.ai/workload",
            "operator": "Equal",
            "value": "ml-system",
            "effect": "NoSchedule",
        }
    ]


def test_kafka_connect_replaces_the_vulnerable_vendor_http2_jar():
    dockerfile = (ROOT / "images/data/recsys-kafka-connect/Dockerfile").read_text()
    assert "ARG NETTY_VERSION=4.1.136.Final" in dockerfile
    assert "netty-codec-http2-${NETTY_VERSION}.jar" in dockerfile
    assert (
        "rm /usr/share/java/kafka-serde-tools/netty-codec-http2-4.1.133.Final.jar"
        in dockerfile
    )
    assert (
        "test ! -e /usr/share/java/kafka-serde-tools/netty-codec-http2-4.1.133.Final.jar"
        in dockerfile
    )


def test_airflow_runtime_is_pinned_to_the_stable_2_9_control_plane():
    dockerfile = (ROOT / "images/data/recsys-airflow/Dockerfile").read_text()
    rendered_airflow = render("recsys-airflow")
    runtime_verifier = (ROOT / "jenkins/scripts/test/runtime.sh").read_text()

    assert "ARG AIRFLOW_VERSION=2.9.3" in dockerfile
    assert "ARG AIRFLOW_PYTHON_VERSION=3.10" in dockerfile
    assert "ARG PYTHON_VERSION" not in dockerfile
    assert "constraints-${AIRFLOW_VERSION}" in dockerfile
    assert "constraints-${AIRFLOW_PYTHON_VERSION}.txt" in dockerfile
    assert "acryl-datahub-airflow-plugin" not in dockerfile
    assert "patch_datahub_plugin_no_openlineage.py" not in dockerfile
    assert "datahub_" + "airflow_plugin" not in dockerfile
    assert "exec airflow webserver" in rendered_airflow
    assert "exec airflow scheduler" in rendered_airflow
    assert "airflow api-server" not in rendered_airflow
    assert "airflow dag-processor" not in rendered_airflow
    assert rendered_airflow.count("name: AIRFLOW__CORE__EXECUTOR") == 2
    assert "AIRFLOW__" + "DATAHUB" not in rendered_airflow
    assert "AIRFLOW_CONN_" + "DATAHUB" not in rendered_airflow
    assert "component_test_airflow_dag_registered" in runtime_verifier
    assert "airflow dags list --output plain" in runtime_verifier
    assert "airflow dags trigger" not in runtime_verifier
    assert "airflow dags list-runs" not in runtime_verifier
    assert "airflow dags state" not in runtime_verifier

    data_config = render("recsys-data-config")
    for pipeline in ("DP1", "DP2", "DP3", "RAG_ITEM"):
        assert f"{pipeline}_DAG_SCHEDULE" in data_config
    assert "RAG_ITEM_SOURCE_RUN_ID" in data_config
    assert "RAG_ITEM_PIPELINE_RUN_ID" in data_config
    assert "MILVUS_HOST" in data_config
    assert "MILVUS_PORT" in data_config
    assert "DATA_PLATFORM_DAG_SCHEDULE" not in data_config
    assert "BATCH_FEATURE_DAG_SCHEDULE" not in data_config


def test_static_catalog_sdk_is_isolated_to_datahub_ops_runtime():
    dependency_files = [
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
        ROOT / "apps/data-platform/pyproject.toml",
        ROOT / "apps/data-platform/uv.lock",
        ROOT / "apps/ml-system/uv.lock",
    ]
    datahub_image = ROOT / "images/data/recsys-datahub-ops/Dockerfile"
    runtime_images = [
        ROOT / "images/data/recsys-feature-store/Dockerfile",
        ROOT / "images/data/recsys-flink/Dockerfile",
        ROOT / "images/data/recsys-spark-data/Dockerfile",
        ROOT / "images/data/recsys-ingestion/Dockerfile",
        ROOT / "images/data/recsys-rag-indexer/Dockerfile",
    ]
    for path in dependency_files + runtime_images + [datahub_image]:
        assert "openlineage-python" not in path.read_text().lower()
    assert '"acryl-datahub==1.6.0.17"' in dependency_files[0].read_text()
    assert '"acryl-datahub==1.6.0.17"' in dependency_files[2].read_text()
    for path in runtime_images:
        assert "acryl-datahub" not in path.read_text()
        assert "DataProcess" + "Instance" not in path.read_text()
    assert "acryl-datahub==1.6.0.17" in datahub_image.read_text()
    assert "from datahub.sdk import DataHubClient, Dataset, Tag" in datahub_image.read_text()


def test_split_spark_data_image_and_dp_profiles_are_the_batch_contract():
    assert (ROOT / "configs/data-platform/spark/dp1.yaml").is_file()
    assert (ROOT / "configs/data-platform/spark/dp2.yaml").is_file()
    assert (ROOT / "configs/data-platform/spark/dp3.yaml").is_file()
    assert not list((ROOT / "configs/data-platform/spark").glob("batch*.yaml"))
    dag = (
        ROOT
        / "apps/data-platform/src/orchestration/airflow/dags/recsys_dp3_offline_feature_table.py"
    ).read_text()
    assert "DP3_CONFIG" in dag
    assert "SPARK_BATCH" not in dag
    assert "dp3_offline_feature_entrypoint.py" in dag


def test_split_spark_images_isolate_the_three_domain_capabilities():
    runtime = (ROOT / "images/data/recsys-spark-runtime/Dockerfile").read_text()
    data = (ROOT / "images/data/recsys-spark-data/Dockerfile").read_text()
    analytics = (ROOT / "images/analytics/recsys-spark-analytics/Dockerfile").read_text()
    ml = (ROOT / "images/ml/recsys-spark-ml/Dockerfile").read_text()
    smoke = (ROOT / "jenkins/scripts/test/spark_image.sh").read_text()
    for artifact in (
        "iceberg-spark-runtime-3.5_2.12",
        "hudi-spark3.5-bundle_2.12",
        "hadoop-aws",
        "aws-java-sdk-bundle",
        "postgresql-${POSTGRES_JDBC_VERSION}.jar",
    ):
        assert artifact in runtime
    assert "python3.10-venv" in runtime
    assert "COPY apps/data-platform/src " in data
    assert "apps/ml-system/src" not in data
    assert "apps/analytics/src" not in data
    assert "COPY apps/analytics/src " in analytics
    assert "COPY apps/data-platform/src/validate /opt/recsys/validate" in analytics
    assert "COPY apps/data-platform/src /opt" not in analytics
    assert "COPY apps/ml-system/src/cli/prepare_bst_training_data.py " in ml
    assert "COPY apps/ml-system/src/cli/create_hudi_savepoint.py " in ml
    assert "COPY apps/ml-system/src/lineage/dataset_versioning.py " in ml
    assert "COPY apps/ml-system/src /opt" not in ml
    assert "apps/data-platform/data-generator" not in ml
    assert "numpy==2.2.6" in ml
    assert "numpy==2.4.6" not in ml
    assert "scikit-learn==1.9.0" not in ml
    for dockerfile in (data, analytics, ml):
        assert "PYSPARK_PYTHON=/opt/venv/bin/python" in dockerfile
        assert "PYSPARK_DRIVER_PYTHON=/opt/venv/bin/python" in dockerfile
    for dockerfile in (data, analytics):
        assert "pyarrow==25.0.0" in dockerfile
        assert "pyarrow==24.0.0" not in dockerfile
    assert "pyarrow==24.0.0" in ml
    assert "pyarrow==25.0.0" not in ml
    for excluded_dependency in ("torch==", "ray[", "mlflow==", "kfp==", "onnx=="):
        assert excluded_dependency not in ml
    assert "generator_config" in smoke
    assert "cli.prepare_bst_training_data" in smoke
    assert "sync_silver" in smoke
    assert "hudi-spark3.5-bundle_2.12-1.2.0.jar" in smoke
    assert "netty-codec-http2-4.1.136.Final.jar" in smoke
    assert "postgresql-42.7.12.jar" in smoke
    assert "hudi-spark3.5-bundle_2.12-1.0.2.jar" not in smoke
    assert "postgresql-42.7.7.jar" not in smoke


def test_feature_store_image_matches_feast_sqlalchemy_registry_driver():
    dockerfile = (ROOT / "images/data/recsys-feature-store/Dockerfile").read_text()
    registry = (
        ROOT
        / "apps/data-platform/feature-store/runtime/src/recsys_feature_store_runtime/sql_registry_state.py"
    ).read_text()
    serving_project = (
        ROOT / "apps/api-serving/online-feature-api/pyproject.toml"
    ).read_text()
    assert "psycopg2-binary" in dockerfile
    assert "psycopg2-binary" in serving_project
    assert 'drivername="postgresql+psycopg2"' in registry
    assert 'drivername="postgresql+psycopg"' not in registry


def test_feature_store_chart_bootstraps_sql_registry_schema():
    rendered = render("recsys-feature-store")
    assert "feature-postgres-schema-init" in rendered
    assert "CREATE SCHEMA IF NOT EXISTS" in rendered
    assert "SET search_path TO" in rendered


def test_runtime_images_expose_the_src_layout_feature_store_package():
    package_src = "/opt/recsys/apps/data-platform/feature-store/runtime/src"
    dockerfiles = (
        "images/data/recsys-rag-indexer/Dockerfile",
        "images/data/recsys-rag-admin/Dockerfile",
        "images/data/recsys-feature-store/Dockerfile",
        "images/data/recsys-drift-retrain/Dockerfile",
        "images/data/recsys-spark-data/Dockerfile",
        "images/ml/recsys-spark-ml/Dockerfile",
        "images/ml/recsys-mlops-training/Dockerfile",
    )
    for path in dockerfiles:
        assert package_src in (ROOT / path).read_text(), path
    for path in (
        "infra/helm/recsys-data-config/templates/configmap.yaml",
        "apps/data-platform/src/orchestration/airflow/spark_utils.py",
        "apps/data-platform/src/orchestration/airflow/dags/recsys_dp1_raw_to_bronze.py",
    ):
        assert package_src in (ROOT / path).read_text(), path

    runtime_root = "/opt/recsys/apps/data-platform/feature-store/runtime"
    assert runtime_root in (
        ROOT / "images/serving/recsys-online-feature-api/Dockerfile"
    ).read_text()


def test_runtime_images_expose_the_src_layout_rag_package():
    package_src = "/opt/recsys/apps/data-platform/rag-runtime"
    for path in (
        "images/data/recsys-rag-indexer/Dockerfile",
        "images/serving/recsys-rag-api/Dockerfile",
    ):
        assert package_src in (ROOT / path).read_text(), path
    for path in (
        "infra/helm/recsys-data-config/templates/configmap.yaml",
        "apps/data-platform/src/orchestration/airflow/spark_utils.py",
    ):
        assert package_src in (ROOT / path).read_text(), path


def test_drift_retrain_image_matches_the_data_platform_setuptools_pin():
    data_project = (ROOT / "apps/data-platform/pyproject.toml").read_text()
    dockerfile = (ROOT / "images/data/recsys-drift-retrain/Dockerfile").read_text()

    assert '"setuptools==80.9.0"' in data_project
    assert "setuptools==80.9.0" in dockerfile
    assert "setuptools==81.0.0" not in dockerfile


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
        assert authorization["spec"]["rules"][0]["to"][0]["operation"]["ports"] == [
            str(port)
        ]

        network_policy = resources[
            ("NetworkPolicy", f"recsys-dataflow-{store}-ingress")
        ]
        ingress = network_policy["spec"]["ingress"][0]
        assert {
            item["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for item in ingress["from"]
        } == allowed_namespaces
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
    assert "componentPipeline.detectReleasePlan()" in jenkinsfile
    assert "--plan-output .ci-release-plan.json" in groovy
    assert "release_build_publish.sh .ci-release-plan.json" in groovy
    assert "deployReleasePlan" in groovy
    assert "runComponentDeployBranches" not in groovy


def test_terraform_bootstraps_split_releases_but_jenkins_owns_runtime_updates():
    terraform = (ROOT / "infra/terraform/gcp/modules/kubernetes-platform/recsys_services.tf").read_text()
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
    assert 'resource "helm_release" "recsys_online_feature_api"' in terraform
    assert 'resource "helm_release" "recsys_inference_api"' in terraform
    assert terraform.count("ignore_changes = all") >= 10
    locals_source = (ROOT / "infra/terraform/gcp/modules/kubernetes-platform/locals.tf").read_text()
    assert '"secret.create"        = "false"' in locals_source
    assert '"kserve.secret.create"' in locals_source
    assert "local.images.online_feature_api" in locals_source
    assert "local.images.inference_api" in locals_source
