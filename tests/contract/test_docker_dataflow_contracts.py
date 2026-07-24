from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import yaml

from config.storage_paths import lakehouse_warehouse_uri, offline_feature_uri, raw_uri


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOTS = [
    ROOT / "apps/data-platform",
    ROOT / "infra/helm/recsys-data-platform",
    ROOT / "infra/docker",
    ROOT / "configs/local",
]
LEGACY_TOKENS = [
    "great_expectations",
    "dbt-core",
    "kafka-minio",
    "s3-sink",
    "bronze/kafka",
    "validate_bronze",
    "spark_realtime_bronze",
    "local_poc",
]


def _runtime_text() -> str:
    chunks: list[str] = []
    for root in RUNTIME_ROOTS:
        if root.is_file():
            chunks.append(root.read_text(encoding="utf-8"))
            continue
        for path in root.rglob("*"):
            if ".venv" in path.parts:
                continue
            if path.is_file() and path.suffix in {
                ".py",
                ".yaml",
                ".yml",
                ".json",
                ".md",
                ".sh",
                ".Dockerfile",
            }:
                chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def test_lakehouse_path_builders_point_to_iceberg_feature_store():
    assert (
        raw_uri("run1", "behavior_events")
        == "s3a://recsys-lakehouse/raw/run1/behavior_events"
    )
    assert lakehouse_warehouse_uri() == "s3a://recsys-lakehouse/warehouse"
    assert (
        offline_feature_uri("item_features")
        == "s3a://recsys-offline-feature-store/warehouse/feature_store/item_features"
    )


def test_no_legacy_runtime_tokens_remain():
    text = _runtime_text()
    for token in LEGACY_TOKENS:
        assert token not in text


def test_debezium_is_the_only_kafka_connect_runtime_connector():
    registrar = (
        ROOT / "apps/data-platform/src/ingest/register_k8s_connectors.py"
    ).read_text()
    connector = json.loads(
        (ROOT / "infra/docker/debezium/postgres-connector.json").read_text()
    )
    kafka_connect_dockerfile = (
        ROOT / "infra/docker/Dockerfile.kafka-connect"
    ).read_text()
    assert '"debezium": ("recsys-postgres-cdc", debezium_config)' in registrar
    assert "recsys-kafka-minio-raw-sink" not in registrar
    assert (
        connector["config"]["connector.class"]
        == "io.debezium.connector.postgresql.PostgresConnector"
    )
    assert "debezium/debezium-connector-postgresql" in kafka_connect_dockerfile
    assert "kafka-connect-s3" not in kafka_connect_dockerfile


def test_spark_and_flink_images_include_runtime_dependencies_without_pandas():
    spark_dockerfile = (ROOT / "apps/data-platform/Dockerfile.spark").read_text()
    flink_dockerfile = (ROOT / "apps/data-platform/Dockerfile.flink").read_text()
    flink_runtime_pom = (ROOT / "apps/data-platform/flink-runtime-pom.xml").read_text()
    dataflow_cli = (ROOT / "apps/data-platform/Dockerfile.dataflow-cli").read_text()
    assert "iceberg-spark-runtime-3.5_2.12" in spark_dockerfile
    assert "hudi-spark3.5-bundle_2.12" in spark_dockerfile
    assert "flink:2.2.0-java17" in flink_dockerfile
    assert "flink-connector-kafka" in flink_runtime_pom
    assert "flink-statebackend-rocksdb" in flink_runtime_pom
    assert "flink-autoscaler-standalone" in flink_dockerfile
    assert "apache-beam==2.61.0" in flink_dockerfile
    assert "avro==1.12.0" in flink_dockerfile
    assert "avro-python3" not in flink_dockerfile
    assert "psycopg[binary]" in flink_dockerfile
    assert "google-cloud-bigquery" not in flink_dockerfile
    assert "great_expectations" not in dataflow_cli
    assert "dbt-core" not in dataflow_cli
    assert " pandas" not in spark_dockerfile
    assert "COPY infra/kubeflow /opt/recsys/infra/kubeflow" in dataflow_cli


def test_kubeflow_training_package_uses_pullable_images():
    package = (ROOT / "infra/kubeflow/compiled/bst_training_pipeline.yaml").read_text()
    assert "recsys-mlops-training:local" not in package
    assert "recsys-mlops-spark:local" not in package
    assert (
        "asia-southeast1-docker.pkg.dev/rec-sys-503309/recsys/recsys-mlops-training:"
        in package
    )
    assert (
        "asia-southeast1-docker.pkg.dev/rec-sys-503309/recsys/recsys-mlops-spark:"
        in package
    )


def test_kubeflow_cloudbuild_builds_compiles_uploads_and_validates_package():
    cloudbuild = (ROOT / "infra/cloudbuild/recsys-feast-kfp.yaml").read_text()

    assert "${_IMAGE_REPO}/recsys-mlops-training:${_TAG}" in cloudbuild
    assert "${_IMAGE_REPO}/recsys-mlops-spark:${_TAG}" in cloudbuild
    assert "id: compile-kfp-package" in cloudbuild
    assert "jenkins/scripts/kubeflow_pipeline_cicd.sh" in cloudbuild
    assert "id: dataflow-cli" in cloudbuild
    assert "id: validate-dataflow-kfp-package" in cloudbuild
    assert "id: upload-kfp-package" in cloudbuild
    assert "_UPLOAD_KFP_PACKAGE" in cloudbuild
    assert cloudbuild.index("id: compile-kfp-package") < cloudbuild.index(
        "id: dataflow-cli"
    )


def test_full_image_cloudbuild_builds_all_runtime_images_after_kfp_compile():
    cloudbuild = (ROOT / "infra/cloudbuild/recsys-images.yaml").read_text()

    for image in [
        "recsys-dataflow-cli",
        "recsys-data-generator",
        "recsys-mlops-training",
        "recsys-mlops-spark",
        "recsys-api-serving",
        "recsys-kafka-connect",
        "recsys-mlflow",
        "recsys-airflow",
        "recsys-spark",
        "recsys-flink",
    ]:
        assert f"${{_IMAGE_REPO}}/{image}:${{_TAG}}" in cloudbuild

    assert "id: compile-kfp-package" in cloudbuild
    assert "id: validate-dataflow-kfp-package" in cloudbuild
    assert "! grep -F ':local'" in cloudbuild
    assert cloudbuild.index("id: compile-kfp-package") < cloudbuild.index(
        "id: dataflow-cli"
    )


def test_remaining_runtime_dockerfiles_use_multistage_and_parallel_tools():
    kafka_connect = (ROOT / "infra/docker/Dockerfile.kafka-connect").read_text()
    mlflow = (ROOT / "infra/docker/Dockerfile.mlflow").read_text()
    mlops_spark = (ROOT / "apps/ml-system/Dockerfile.spark").read_text()

    assert "FROM confluentinc/cp-kafka-connect:7.5.0 AS plugins" in kafka_connect
    assert "FROM confluentinc/cp-kafka-connect:7.5.0 AS runtime" in kafka_connect
    assert "CONNECTOR_INSTALL_JOBS" in kafka_connect
    assert "job_count=0" in kafka_connect
    assert (
        'confluent-hub install --no-prompt --component-dir /tmp/confluent-hub-components "${connector}" &'
        in kafka_connect
    )
    assert "COPY --from=plugins /tmp/confluent-hub-components" in kafka_connect
    assert "debezium/debezium-connector-postgresql" in kafka_connect
    assert "kafka-connect-s3" not in kafka_connect

    assert "FROM python:3.11-slim AS deps" in mlflow
    assert "FROM python:3.11-slim AS runtime" in mlflow
    assert "UV_CONCURRENT_DOWNLOADS=8" in mlflow
    assert "UV_CONCURRENT_BUILDS=8" in mlflow
    assert "apt-get install -y --no-install-recommends bash" in mlflow
    assert "ln -s /opt/venv/bin/mlflow /usr/local/bin/mlflow" in mlflow
    assert "COPY --from=deps /opt/venv /opt/venv" in mlflow

    mlflow_chart = (ROOT / "infra/helm/mlflow-stack/templates/mlflow.yaml").read_text()
    assert "/opt/venv/bin/mlflow server" in mlflow_chart

    assert " AS deps" in mlops_spark
    assert " AS runtime" in mlops_spark
    assert "UV_CONCURRENT_DOWNLOADS=8" in mlops_spark
    assert "UV_CONCURRENT_BUILDS=8" in mlops_spark
    assert "boto3" in mlops_spark
    assert "psycopg[binary]" in mlops_spark
    assert "psycopg-pool" in mlops_spark
    assert "COPY --from=deps /opt/venv /opt/venv" in mlops_spark
    assert "PYSPARK_PYTHON=/opt/venv/bin/python" in mlops_spark


def test_jenkins_training_component_builds_runtime_images_and_package_trigger_image():
    build_script = (ROOT / "jenkins/scripts/component_build_publish.sh").read_text()
    training_case = build_script.split("training)", 1)[1].split(";;", 1)[0]

    assert "build_training" in training_case
    assert "build_mlops_spark" in training_case
    assert "compile_kfp_package_for_image_refs" in training_case
    assert "build_dataflow_cli" in training_case
    assert training_case.index(
        "compile_kfp_package_for_image_refs"
    ) < training_case.index("build_dataflow_cli")


def test_jenkins_training_deploy_uploads_package_and_rolls_trigger_runtime():
    deploy_script = (ROOT / "jenkins/scripts/component_deploy.sh").read_text()
    assert "jenkins/scripts/kubeflow_pipeline_cicd.sh" in deploy_script
    assert '--set "images.dataflowCli=${dataflow_image}"' in deploy_script
    assert (
        'verify_data_platform_config_image "DATAFLOW_IMAGE" "${dataflow_image}"'
        in deploy_script
    )
    assert (
        'verify_and_wait_workload "deployment" "realtime-event-producer"'
        in deploy_script
    )


def test_full_services_cicd_runs_all_stages_and_post_deploy_e2e():
    script = (ROOT / "jenkins/scripts/full_services_cicd.sh").read_text()
    build_script = (ROOT / "jenkins/scripts/component_build_publish.sh").read_text()
    deploy_script = (ROOT / "jenkins/scripts/component_deploy.sh").read_text()

    assert "component_ci.sh" in script
    assert "component_build_publish.sh all" in script
    assert "component_deploy.sh all" in script
    assert "cluster_data_setup.sh" in script
    assert "cluster_mlops_serving_e2e.sh" in script
    assert "post_deploy_e2e.sh" in script
    assert "RUN_NODE_REBALANCE" in script
    assert "VALIDATE_NODE_REBALANCE" in script
    assert "FULL_CICD_BUILD_BACKEND:-docker" in script
    assert "cloudbuild)" in script
    assert "all)" in build_script
    assert "build_mlflow" in build_script
    assert "deploy_all()" in deploy_script
    assert "run_node_rebalance_if_enabled" in deploy_script
    assert "infra/k8s/scripts/rebalance_ml_node_pool.sh" in deploy_script
    assert "jenkins/scripts/validate_node_rebalance.sh" in deploy_script
    assert "deploy_mlflow" in deploy_script
    assert '--set "nodeSelector.recsys\\\\.ai/pool=ml-system"' in deploy_script
    assert '--set "minio.resources.requests.memory=512Mi"' in deploy_script
    assert "kfp_endpoint_for_upload()" in deploy_script
    assert "kubectl port-forward" in deploy_script
    assert (
        "observability.retrainPsiThreshold=${RETRAIN_PSI_THRESHOLD:-0.15}"
        in deploy_script
    )


def test_node_rebalance_validation_covers_relocated_control_plane():
    validator = (ROOT / "jenkins/scripts/validate_node_rebalance.sh").read_text()
    rebalance = (ROOT / "infra/k8s/scripts/rebalance_ml_node_pool.sh").read_text()
    power_script = (
        ROOT / "infra/terraform/gcp/scripts/gcp_services_power.sh"
    ).read_text()

    for expected in [
        "kubeflow ml-pipeline",
        "kserve kserve-controller-manager",
        "ci recsys-jenkins",
        "experiment-tracking minio",
        "kube-system metrics-server-v1.35.1",
    ]:
        assert expected in validator
    assert "kube-system kube-dns" in validator
    assert "assert_deployment_selector ci recsys-jenkins" in validator
    assert "sidecar.istio.io/inject" in validator
    assert "assert_no_local_images" in validator
    assert "assert_no_bad_pods" in validator
    assert "kube_system_ml_deployments" in rebalance
    assert "patch_gke_managed_deployment_cpu kube-dns" in rebalance
    assert "patch_deployment_cpu ci" in rebalance
    assert "ci_cpu_deployments" in rebalance
    assert "enable_ingress_mesh_upstreams" in rebalance
    assert (
        "assert_istio_sidecar_enabled deployment ingress-nginx ingress-nginx-controller"
        in validator
    )
    assert "kubectl patch deployment ingress-nginx-controller" in power_script
    assert '"sidecar.istio.io/inject": "true"' in power_script
    assert (
        "disable_sidecar_injection daemonset observability recsys-promtail" in rebalance
    )


def test_airflow_keeps_rubric_and_key_operational_dag_modules():
    dag_dir = ROOT / "apps/data-platform/src/orchestration/airflow/dags"
    assert {path.name for path in dag_dir.glob("*.py")} == {
        "__init__.py",
        "k8s_data_platform_dag.py",
        "rubric_data_pipeline_dags.py",
    }
    source = (dag_dir / "rubric_data_pipeline_dags.py").read_text()
    assert 'dag_id="recsys_dp1_raw_to_bronze"' in source
    assert 'dag_id="recsys_dp2_bronze_to_silver_gold"' in source
    assert 'dag_id="recsys_dp3_offline_feature_table"' in source
    assert "recsys_lakehouse_maintenance" not in source
    assert source.count("ingest_stage >> optimize_stage >> validate_stage") == 2
    assert "--scope bronze" in source
    assert "--scope silver" in source
    assert "python3 apps/data-platform/data-generator/src/cli.py generate" in source


def test_retrain_trigger_uses_distinct_tune_and_ddp_results_for_default_drift_runs():
    source = (
        ROOT / "apps/data-platform/src/mlops/trigger_kubeflow_retrain.py"
    ).read_text()

    assert 'ray_tune_result_path = f"{base}/ml/ray/tune_result.json"' in source
    assert 'ray_best_result_path = f"{base}/ml/ray/best_result.json"' in source
    assert '"ray_tune_result_path": ray_tune_result_path' in source
    assert '"ray_best_result_path": ray_best_result_path' in source
    assert '"feature_source": "offline_feature_store"' in source
    assert '"distributed_worker_replicas": 2' in source
    assert '"distributed_num_workers": 2' in source
    assert '"max_trials": 1' in source
    assert '"cpus_per_trial": 0.5' in source
    assert '"ray_ttl_seconds_after_finished": 60' in source


def test_retrain_trigger_uses_stable_safe_kfp_run_names():
    source = (
        ROOT / "apps/data-platform/src/mlops/trigger_kubeflow_retrain.py"
    ).read_text()

    assert 'os.getenv("KFP_RETRAIN_RUN_NAME_PREFIX", "recsys-drift-retrain")' in source
    assert "def kfp_run_name(run_id: str, prefix: str | None = None) -> str:" in source
    assert "run_name=kfp_run_name(run_id)" in source
    assert 'run_name=f"recsys-drift-retrain-{run_id}"' not in source
    assert '"pipeline_run_id": f"retrain-{slug}"' in source


def test_k8s_airflow_spark_tasks_use_native_kubernetes_mode():
    source = (
        ROOT
        / "apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py"
    ).read_text()
    assert 'cmds=["bash", "-c"]' in source
    assert 'cmds=["bash", "-lc"]' not in source
    for expected in [
        "--master ${SPARK_K8S_MASTER:-k8s://https://kubernetes.default.svc}",
        "--deploy-mode cluster",
        "spark.kubernetes.container.image=${SPARK_K8S_IMAGE:-recsys-spark:local}",
        "spark.kubernetes.authenticate.driver.serviceAccountName",
        "spark.driver.memoryOverhead=${SPARK_K8S_DRIVER_MEMORY_OVERHEAD:-384m}",
        "spark.executor.instances=${SPARK_K8S_EXECUTOR_INSTANCES:-1}",
        "spark.executor.memoryOverhead=${SPARK_K8S_EXECUTOR_MEMORY_OVERHEAD:-384m}",
        "spark.kubernetes.submission.waitAppCompletion=true",
        "spark.kubernetes.submission.connectionTimeout=${SPARK_K8S_CONNECTION_TIMEOUT:-60000}",
        "spark.kubernetes.submission.requestTimeout=${SPARK_K8S_REQUEST_TIMEOUT:-180000}",
        "spark.sql.iceberg.vectorization.enabled=${SPARK_ICEBERG_VECTORIZATION_ENABLED:-false}",
        "local:///opt/recsys/apps/data-platform/src/features/spark/spark_batch_entrypoint.py",
    ]:
        assert expected in source
    assert 'SPARK_SUBMIT_LOG="$(mktemp)"' in source
    assert 'grep -q "phase: Succeeded" "$SPARK_SUBMIT_LOG"' in source


def test_airflow_native_spark_submissions_configure_dynamic_allocation():
    dag_paths = (
        "apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py",
    )
    expected = (
        "spark.dynamicAllocation.enabled=${SPARK_DYNAMIC_ALLOCATION_ENABLED:-false}",
        "spark.dynamicAllocation.shuffleTracking.enabled=${SPARK_DYNAMIC_ALLOCATION_SHUFFLE_TRACKING_ENABLED:-true}",
        "spark.dynamicAllocation.minExecutors=${SPARK_DYNAMIC_ALLOCATION_MIN_EXECUTORS:-1}",
        "spark.dynamicAllocation.initialExecutors=${SPARK_DYNAMIC_ALLOCATION_INITIAL_EXECUTORS:-1}",
        "spark.dynamicAllocation.maxExecutors=${SPARK_DYNAMIC_ALLOCATION_MAX_EXECUTORS:-4}",
        "spark.dynamicAllocation.executorIdleTimeout=${SPARK_DYNAMIC_ALLOCATION_EXECUTOR_IDLE_TIMEOUT:-60s}",
        "spark.dynamicAllocation.schedulerBacklogTimeout=${SPARK_DYNAMIC_ALLOCATION_SCHEDULER_BACKLOG_TIMEOUT:-1s}",
        "spark.dynamicAllocation.sustainedSchedulerBacklogTimeout=${SPARK_DYNAMIC_ALLOCATION_SUSTAINED_BACKLOG_TIMEOUT:-1s}",
    )

    for dag_path in dag_paths:
        source = (ROOT / dag_path).read_text()
        for setting in expected:
            assert setting in source


def test_dp1_batch_ingestion_commits_bronze_iceberg_with_spark():
    dag = (
        ROOT
        / "apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py"
    ).read_text()
    ingestion_source = (
        ROOT / "apps/data-platform/src/ingest/batch_lakehouse_ingestion.py"
    ).read_text()
    dp2_source = (
        ROOT / "apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py"
    ).read_text()
    assert "/opt/spark/bin/spark-submit" in dag
    assert "batch_lakehouse_ingestion.py" in dag
    assert "write_iceberg_table" in ingestion_source
    assert "catalog.bronze_table" in ingestion_source or "bronze_" in ingestion_source
    assert 'source="lakehouse"' in dp2_source
    assert 'source="parquet"' not in dp2_source


def test_k8s_airflow_task_pods_can_skip_istio_mesh_for_native_jobs():
    source = (
        ROOT
        / "apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py"
    ).read_text()
    assert '"sidecar.istio.io/inject": "false"' in source
    assert (
        "curl --max-time 5 -sf -X POST http://127.0.0.1:15020/quitquitquit"
        not in source
    )
    assert "startup_timeout_seconds=600" in source


def test_airflow_runtime_disables_bytecode_writes_for_non_root_user():
    dockerfile = (ROOT / "infra/docker/Dockerfile.airflow").read_text()
    chart = (
        ROOT / "infra/helm/recsys-data-platform/templates/airflow.yaml"
    ).read_text()
    assert "ENV PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE" in chart
    assert "PATH=/home/airflow/.local/bin:${PATH}" in dockerfile
    assert "chown -R airflow:root /home/airflow" in dockerfile
    assert 'command: ["bash", "-c"]' in chart
    assert 'command: ["bash", "-lc"]' not in chart
    assert (
        "timeout 120 airflow db check-migrations --migration-wait-timeout 120 || true"
        in chart
    )
    assert "airflow db migrate &&" not in chart
    assert 'value: "900"' in chart


def test_component_deploy_preserves_spark_byte_size_as_integer_string():
    deploy = (ROOT / "jenkins/scripts/component_deploy.sh").read_text()

    assert '--set-string "spark.advisoryPartitionSizeBytes=' in deploy
    assert '--set "spark.advisoryPartitionSizeBytes=' not in deploy


def test_airflow_image_packages_data_and_analytics_dags():
    dockerfile = (ROOT / "infra/docker/Dockerfile.airflow").read_text()

    assert "COPY --chown=airflow:root apps/data-platform/src" in dockerfile
    assert "apps/analytics/orchestration/airflow/dags" in dockerfile


def test_flink_runtime_uses_fixed_mesh_friendly_internal_ports():
    chart = ROOT / "infra/helm/recsys-data-platform"
    security_chart = ROOT / "infra/helm/recsys-security"
    rendered = "\n".join(
        path.read_text() for path in (chart / "templates").glob("*.yaml")
    )
    values = yaml.safe_load((chart / "values.yaml").read_text())
    security_rendered = "\n".join(
        path.read_text() for path in (security_chart / "templates").glob("*.yaml")
    )
    for expected in [
        "jobmanager.rpc.port: 6123",
        "blob.server.port: 6124",
        "taskmanager.data.port: 6121",
        "taskmanager.rpc.port: 6122",
        "query.server.port: 6125",
        "pekko.remote.startup-timeout: 60 s",
        "containerPort: 6121",
        "containerPort: 6122",
        "containerPort: 6125",
        "type: Recreate",
    ]:
        assert expected in rendered
    assert values["flinkTaskManager"]["resources"] == {
        "requests": {"cpu": "500m", "memory": "4Gi"},
        "limits": {"cpu": "2", "memory": "8Gi"},
    }
    assert values["flink"]["taskManagerProcessMemory"] == "6144m"
    assert values["flink"]["taskManagerTaskHeapMemory"] == "2048m"
    assert values["flink"]["taskManagerManagedMemory"] == "2048m"
    assert values["flink"]["taskManagerJvmOverheadMax"] == "2048m"
    assert values["flink"]["stateBackend"] == "rocksdb"
    assert values["flink"]["stateBackendIncremental"] == "true"
    assert values["flink"]["pythonManagedMemory"] == "true"
    assert values["flink"]["pythonBundleSize"] == "1000"
    assert values["flink"]["pythonBundleTimeMs"] == "200"
    assert values["flink"]["disableJemalloc"] is True
    assert "name: DISABLE_JEMALLOC" in rendered
    assert "name: JDK_JAVA_OPTIONS" in rendered
    assert "taskmanager.memory.jvm-overhead.max" in rendered
    assert '"6121", "6122", "6123", "6124", "6125"' in security_rendered


def test_flink_starts_at_parallelism_one_and_autoscales_sustained_backlog():
    chart = ROOT / "infra/helm/recsys-data-platform"
    values = yaml.safe_load((chart / "values.yaml").read_text())
    rendered = "\n".join(
        path.read_text() for path in (chart / "templates").glob("*.yaml")
    )

    assert values["realtimeFlinkConsumer"]["parallelism"] == "1"
    assert values["realtimeFlinkConsumer"]["asyncIoCapacity"] == "64"
    assert values["realtimeFlinkConsumer"]["asyncIoTimeoutSeconds"] == "120"
    assert values["streaming"]["featureWindowSeconds"] == 60
    assert values["streaming"]["featureEarlyFireSeconds"] == 5
    assert values["flinkAutoscaler"]["enabled"] is True
    assert values["flinkAutoscaler"]["scalingEnabled"] is True
    assert values["flinkAutoscaler"]["version"] == "1.15.0"
    assert values["flink"]["scheduler"] == "adaptive"
    assert values["flinkAutoscaler"]["vertexMinParallelism"] == "1"
    assert values["flinkAutoscaler"]["vertexMaxParallelism"] == "4"
    assert values["flinkAutoscaler"]["taskManagerHpa"] == {
        "enabled": True,
        "minReplicas": 2,
        "maxReplicas": 4,
        "targetCpuUtilization": 65,
        "scaleDownStabilizationSeconds": 300,
    }
    assert "jobmanager.scheduler: %s" in rendered
    assert ".Values.flink.scheduler | quote" in rendered
    assert "cluster.declarative-resource-management.enabled: true" in rendered
    assert "kind: HorizontalPodAutoscaler" in rendered
    assert "StandaloneAutoscalerEntrypoint" in rendered
    assert "job.autoscaler.utilization.target" in rendered
    assert "job.autoscaler.vertex.min-parallelism" in rendered
    assert "job.autoscaler.vertex.max-parallelism" in rendered
    assert "pipeline.max-parallelism" in rendered
    stream_job = (
        ROOT / "apps/data-platform/src/features/flink/realtime_stream_job.py"
    ).read_text()
    sink_dir = ROOT / "apps/data-platform/src/features/flink/sinks"
    stream_sinks = (sink_dir / "redis_async.py").read_text() + (
        sink_dir / "postgres_async.py"
    ).read_text()
    checkpoint_config = (
        ROOT / "apps/data-platform/src/features/flink/runtime.py"
    ).read_text()
    assert "AsyncDataStream.unordered_wait" in stream_job
    assert "capacity=postgres_async_capacity(args)" in stream_job
    assert "timeout=float(args.async_io_timeout_seconds)" in stream_sinks
    assert 'restore_args=(-s "$FLINK_RESTORE_PATH")' in rendered
    assert '"${restore_args[@]}"' in rendered
    assert stream_sinks.count("def timeout(") >= 3
    assert "postgres_feast_offline_timeout" in stream_sinks
    assert "ExternalizedCheckpointRetention" in checkpoint_config
    assert "set_externalized_checkpoint_retention" in checkpoint_config
    assert "--async-io-capacity" in rendered
    assert "--feature-window-seconds" in rendered
    assert "--feature-early-fire-seconds" in rendered
    assert "--redis-sink-max-events-per-second" in rendered
    assert "--postgres-sink-max-events-per-second" in rendered


def test_flink_modules_do_not_define_classes_inside_functions():
    flink_root = ROOT / "apps/data-platform/src/features/flink"
    violations = []
    for path in flink_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            for child in ast.walk(function):
                if isinstance(child, ast.ClassDef):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{child.lineno} {child.name}"
                    )
    assert violations == []


def test_flink_module_boundaries_match_runtime_responsibilities():
    flink_root = ROOT / "apps/data-platform/src/features/flink"
    expected = {
        "runtime.py",
        "source.py",
        "operators/dedup.py",
        "operators/late_policy.py",
        "operators/quality.py",
        "operators/row_mappers.py",
        "features/user_sequence.py",
        "features/user_aggregate.py",
        "features/item.py",
        "features/candidate_pool.py",
        "sinks/redis_async.py",
        "sinks/postgres_async.py",
        "sinks/rate_limit.py",
        "sinks/iceberg.py",
    }
    assert all((flink_root / relative_path).is_file() for relative_path in expected)
    assert not (flink_root / "runtime_config.py").exists()
    assert not (flink_root / "quality_windows.py").exists()
    assert not (flink_root / "iceberg_feature_sink.py").exists()

    entrypoint = flink_root / "realtime_stream_job.py"
    result = subprocess.run(
        [sys.executable, str(entrypoint), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--feature-window-seconds" in result.stdout


def test_helm_exposes_iceberg_lakehouse_runtime_config():
    chart = ROOT / "infra/helm/recsys-data-platform"
    values = yaml.safe_load((chart / "values.yaml").read_text())
    rendered = (chart / "values.yaml").read_text() + "\n".join(
        path.read_text() for path in (chart / "templates").glob("*.yaml")
    )
    assert values["lakehouse"]["catalog"] == "recsys"
    assert values["lakehouse"]["lakehouseNamespace"] == "lakehouse"
    assert values["lakehouse"]["offlineFeatureCatalog"] == "recsys_features"
    assert values["lakehouse"]["featureNamespace"] == "feature_store"
    assert values["realtimeCdcConnector"]["enabled"] is True
    assert values["e2e"]["realtimeEnabled"] == "true"
    assert values["e2e"]["datahubIngestEnabled"] == "false"
    assert values["realtimeFlinkConsumer"]["offlineStoreSink"] == "postgres"
    assert (
        values["realtimeFlinkConsumer"]["online"]["startingOffsets"]
        == "committed-offsets"
    )
    assert values["featurePostgres"]["name"] == "feature-postgres"
    assert values["featurePostgres"]["schema"] == "feature_store"
    assert "LAKEHOUSE_WAREHOUSE" in rendered
    assert "ICEBERG_CATALOG" in rendered
    assert "ICEBERG_LAKEHOUSE_NAMESPACE" in rendered
    assert "OFFLINE_FEATURE_STORE_WAREHOUSE" in rendered
    assert "OFFLINE_FEATURE_CATALOG" in rendered
    assert "OFFLINE_STORE_ENABLED" in rendered
    assert "OFFLINE_STORE_SINK" in rendered
    assert '--offline-store-sink "$OFFLINE_STORE_SINK"' in rendered
    assert (
        '--starting-offsets {{ default "committed-offsets" $consumer.online.startingOffsets | quote }}'
        in rendered
    )
    assert '--feast-postgres-host "$FEAST_POSTGRES_HOST"' in rendered
    assert '--feast-postgres-database "$FEAST_POSTGRES_DB"' in rendered
    assert '--feast-postgres-password "$FEAST_POSTGRES_PASSWORD"' in rendered
    assert "OFFLINE_FEATURE_DRIFT_REPORT_PATH" in rendered
    assert "OFFLINE_FEATURE_DRIFT_CURRENT_ROOT" in rendered
    assert "OFFLINE_FEATURE_DRIFT_BASELINE_PATH" in rendered
    assert "OFFLINE_FEATURE_DRIFT_SAMPLE_ROWS" in rendered
    assert "OFFLINE_FEATURE_DRIFT_TABLES" in rendered
    assert "DATA_PLATFORM_DAG_SCHEDULE" in rendered
    assert "RETRAIN_PSI_THRESHOLD" in rendered
    assert "register-realtime-cdc-connector" in rendered
    assert "--offline-store-enabled" in rendered
    assert "SPARK_K8S_MASTER" in rendered
    assert "SPARK_K8S_EXECUTOR_INSTANCES" in rendered
    assert "SPARK_DYNAMIC_ALLOCATION_ENABLED" in rendered
    assert "SPARK_DYNAMIC_ALLOCATION_SHUFFLE_TRACKING_ENABLED" in rendered
    assert "SPARK_DYNAMIC_ALLOCATION_MIN_EXECUTORS" in rendered
    assert "SPARK_DYNAMIC_ALLOCATION_INITIAL_EXECUTORS" in rendered
    assert "SPARK_DYNAMIC_ALLOCATION_MAX_EXECUTORS" in rendered
    assert "SPARK_K8S_DRIVER_MEMORY_OVERHEAD" in rendered
    assert "SPARK_K8S_EXECUTOR_MEMORY_OVERHEAD" in rendered
    assert "SPARK_ICEBERG_VECTORIZATION_ENABLED" in rendered
    assert "DATA_GENERATOR_CONFIG" in rendered
    assert "SPARK_BATCH_CONFIG" in rendered
    assert "REALTIME_E2E_ENABLED" in rendered
    assert "DATAHUB_INGEST_ENABLED" in rendered
    assert "AWS_ACCESS_KEY_ID" in rendered
    assert "AWS_SECRET_ACCESS_KEY" in rendered
    assert "deletecollection" in rendered
    assert "AIRFLOW__DAG_PROCESSOR__DAG_FILE_PROCESSOR_TIMEOUT" in rendered
    assert "{{- if .Values.realtimeCdcConnector.enabled }}" in rendered


def test_e2e_1k_whole_run_data_setup_configs_are_wired_into_helm_values():
    chart = ROOT / "infra/helm/recsys-data-platform"
    values = yaml.safe_load((chart / "values.yaml").read_text())
    generator = yaml.safe_load(
        (ROOT / values["dataSetup"]["generatorConfig"]).read_text()
    )
    spark_batch = yaml.safe_load(
        (ROOT / values["dataSetup"]["sparkBatchConfig"]).read_text()
    )
    offline_generator = generator["offline"]["generator"]
    assert offline_generator["traffic"]["target_behavior_events"] == 50000
    assert (
        offline_generator["output"]["run_id"]
        == values["dataSetup"]["generatorRunId"]
        == "test_1k_seed42"
    )
    assert spark_batch["input"]["source"] == "silver_lakehouse"
    assert spark_batch["processing"]["mode"] == "whole_run"
    assert values["spark"]["executorInstances"] == "1"
    assert values["spark"]["dynamicAllocation"]["enabled"] is False
    assert values["spark"]["driverMemoryOverhead"] == "128m"
    assert values["spark"]["executorMemoryOverhead"] == "128m"


def test_gcp_data_platform_spark_resources_cover_e2e_batch_workload():
    values = yaml.safe_load(
        (ROOT / "infra/helm/recsys-data-platform/values-gcp.yaml").read_text()
    )
    assert values["spark"]["driverMemory"] == "2g"
    assert values["spark"]["driverMemoryOverhead"] == "1g"
    assert values["spark"]["executorInstances"] == "1"
    assert values["spark"]["executorMemory"] == "1536m"
    assert values["spark"]["executorMemoryOverhead"] == "1536m"
    assert values["spark"]["dynamicAllocation"] == {
        "enabled": False,
        "shuffleTrackingEnabled": True,
        "minExecutors": "1",
        "initialExecutors": "1",
        "maxExecutors": "1",
        "executorIdleTimeout": "60s",
        "schedulerBacklogTimeout": "1s",
        "sustainedSchedulerBacklogTimeout": "1s",
    }
    assert values["flinkTaskManager"]["replicas"] == 2
    assert values["flink"]["taskSlots"] == "1"
    assert values["realtimeFlinkConsumer"]["parallelism"] == "1"


def test_component_deploy_applies_gcp_spark_resources_without_statefulset_value_merge():
    deploy_script = (ROOT / "jenkins/scripts/component_deploy.sh").read_text()
    assert "--reuse-values" in deploy_script
    assert "spark.driverMemory=${SPARK_K8S_DRIVER_MEMORY:-2g}" in deploy_script
    assert (
        "spark.driverMemoryOverhead=${SPARK_K8S_DRIVER_MEMORY_OVERHEAD:-1g}"
        in deploy_script
    )
    assert "spark.executorMemory=${SPARK_K8S_EXECUTOR_MEMORY:-1536m}" in deploy_script
    assert (
        "spark.executorMemoryOverhead=${SPARK_K8S_EXECUTOR_MEMORY_OVERHEAD:-1536m}"
        in deploy_script
    )
    assert (
        "spark.dynamicAllocation.enabled=${SPARK_DYNAMIC_ALLOCATION_ENABLED:-false}"
        in deploy_script
    )
    assert (
        "spark.dynamicAllocation.shuffleTrackingEnabled=${SPARK_DYNAMIC_ALLOCATION_SHUFFLE_TRACKING_ENABLED:-true}"
        in deploy_script
    )
    assert (
        "spark.dynamicAllocation.maxExecutors=${SPARK_DYNAMIC_ALLOCATION_MAX_EXECUTORS:-1}"
        in deploy_script
    )
    assert "kafka.topicPartitions=${KAFKA_TOPIC_PARTITIONS:-4}" in deploy_script
    assert "flinkTaskManager.replicas=${FLINK_TASKMANAGER_REPLICAS:-2}" in deploy_script
    assert "flink.taskSlots=${FLINK_TASK_SLOTS:-1}" in deploy_script
    assert "flink.disableJemalloc=${FLINK_DISABLE_JEMALLOC:-true}" in deploy_script
    assert "flink.scheduler=${FLINK_SCHEDULER:-adaptive}" in deploy_script
    assert "realtimeFlinkConsumer.parallelism=${FLINK_PARALLELISM:-1}" in deploy_script
    assert "flinkAutoscaler.enabled=${FLINK_AUTOSCALER_ENABLED:-true}" in deploy_script
    assert (
        "flinkAutoscaler.vertexMaxParallelism=${FLINK_AUTOSCALER_VERTEX_MAX_PARALLELISM:-4}"
        in deploy_script
    )
    assert (
        "flinkAutoscaler.taskManagerHpa.maxReplicas=${FLINK_TASKMANAGER_HPA_MAX_REPLICAS:-2}"
        in deploy_script
    )
    assert (
        "realtimeFlinkConsumer.redisSinkMaxEventsPerSecond=${REDIS_SINK_MAX_EVENTS_PER_SECOND:-200}"
        in deploy_script
    )
    assert (
        "--values infra/helm/recsys-data-platform/values-gcp.yaml" not in deploy_script
    )


def test_spark_silver_deduplicates_behavior_events_and_impressions_with_drop_duplicates():
    source = (
        ROOT / "apps/data-platform/src/features/spark/build_silver_tables.py"
    ).read_text()

    assert 'supported.dropDuplicates(["event_id"])' in source
    assert '.dropDuplicates(["impression_id"])' in source
    assert '.orderBy("event_timestamp", "event_id")' not in source
    assert 'Window.partitionBy("event_id")' not in source
    assert "F.row_number().over(window)" not in source
    assert '"rejection_reason", F.lit("unsupported_schema_version")' in source


def test_spark_user_aggregate_uses_approximate_rolling_category_cardinality():
    source = (
        ROOT / "apps/data-platform/src/features/spark/build_user_aggregate_features.py"
    ).read_text()

    assert "CATEGORY_CARDINALITY_RSD = 0.05" in source
    assert 'F.approx_count_distinct("category_id", CATEGORY_CARDINALITY_RSD)' in source
    assert 'F.collect_list(F.col("category_id"))' not in source


def test_security_chart_declares_vault_external_secrets_and_istio_policies():
    chart = ROOT / "infra/helm/recsys-security"
    rendered = (chart / "values.yaml").read_text() + "\n".join(
        path.read_text() for path in (chart / "templates").glob("*.yaml")
    )
    for expected in [
        "ClusterSecretStore",
        "external-secrets.io/v1",
        "recsys-data-platform-secret",
        "recsys-mlflow-secrets",
        "recsys-mlops-runtime",
        "recsys-kserve-minio",
        "PeerAuthentication",
        "mode: STRICT",
        "AuthorizationPolicy",
        "recsys-kubeflow-allow",
        "recsys-kubeflow-ml-pipeline-api-allow",
        "recsys-kubeflow-ml-pipeline-permissive",
        "recsys-kubeflow-metadata-grpc-allow",
        "recsys-kubeflow-metadata-grpc-permissive",
        "recsys-kubeflow-seaweedfs-allow",
        "recsys-kubeflow-seaweedfs-permissive",
        "recsys-mlflow-allow",
        "recsys-kserve-allow",
        "namespaces:",
        "- kubeflow",
        "mode: PERMISSIVE",
        '"3306"',
        '"8080"',
        '"8887"',
        '"9000"',
        '"2181"',
        '"29092"',
        "cluster.local/ns/api-serving/sa/default",
        "cluster.local/ns/recsys-dataflow/sa/default",
        "cluster.local/ns/kubeflow/sa/pipeline-runner",
    ]:
        assert expected in rendered


def test_app_charts_do_not_render_literal_runtime_secrets_by_default():
    chart_roots = [
        ROOT / "infra/helm/recsys-data-platform",
        ROOT / "infra/helm/mlflow-stack",
        ROOT / "infra/helm/recsys-runtime",
        ROOT / "infra/helm/recsys-serving",
    ]
    rendered = "\n".join(
        (root / "values.yaml").read_text()
        + "\n".join(path.read_text() for path in (root / "templates").glob("*.yaml"))
        for root in chart_roots
    )
    for forbidden in [
        "rootPassword: minio123",
        "password: mlflow123",
        "secretAccessKey: minio123",
    ]:
        assert forbidden not in rendered
    for expected in [
        "secret:",
        "create: false",
        "recsys-data-platform-secret",
        "recsys-mlflow-secrets",
        "recsys-mlops-runtime",
        "recsys-kserve-minio",
    ]:
        assert expected in rendered


def test_spark_batch_config_reads_dp2_silver_iceberg_tables():
    config = yaml.safe_load((ROOT / "configs/local/spark_batch.yaml").read_text())
    assert config["input"]["source"] == "silver_lakehouse"
    assert config["processing"]["mode"] == "whole_run"
    output = config["output"]
    assert output["lakehouse_warehouse"] == "s3a://recsys-lakehouse/warehouse"
    assert output["iceberg_catalog"] == "recsys"
    assert output["iceberg_lakehouse_namespace"] == "lakehouse"
    assert output["offline_feature_catalog"] == "recsys_features"
    assert (
        output["offline_feature_store_warehouse"]
        == "s3a://recsys-offline-feature-store/warehouse"
    )
    assert output["iceberg_feature_namespace"] == "feature_store"
    assert (
        output["offline_feature_store_uri"]
        == "s3a://recsys-offline-feature-store/warehouse/feature_store"
    )


def test_spark_batch_entrypoint_processes_the_whole_run_in_one_commit():
    source = (
        ROOT / "apps/data-platform/src/features/spark/spark_batch_entrypoint.py"
    ).read_text()
    assert "batch_chunk_count" not in source
    assert "batch_chunk_commits" not in source
    assert "batch_commit_id" not in source
    assert 'write_iceberg_table(frame, table_name, mode="overwrite")' in source
    assert 'os.getenv("OFFLINE_FEATURE_DRIFT_CURRENT_ROOT", "")' in source
    assert '"ml_bst_training",' in source


def test_drift_monitor_reads_a_current_snapshot_not_the_iceberg_data_directory():
    values = yaml.safe_load(
        (ROOT / "infra/helm/recsys-data-platform/values.yaml").read_text()
    )
    assert values["drift"]["currentRoot"].endswith(
        "/monitoring/offline_feature_drift/current_snapshot"
    )
    dag = (
        ROOT
        / "apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py"
    ).read_text()
    assert '"OFFLINE_FEATURE_DRIFT_CURRENT_ROOT",' in dag


def test_deleted_legacy_artifacts_are_absent():
    for relative in [
        "infra/docker/debezium/kafka-connect-s3-sink.json",
        "infra/docker/scripts/register_minio_sink_connector.sh",
        "infra/docker/scripts/validate_bronze_cdc.py",
        "apps/data-platform/great_expectations",
        "apps/data-platform/dbt",
        "apps/data-platform/src/features/spark/spark_realtime_bronze_entrypoint.py",
        "apps/data-platform/src/orchestration/airflow/dags/batch_feature_pipeline_dag.py",
        "apps/data-platform/src/orchestration/airflow/dags/full_dataflow_local_dag.py",
        "apps/data-platform/src/orchestration/airflow/dags/raw_ingestion_dag.py",
        "apps/data-platform/src/orchestration/airflow/dags/streaming_feature_pipeline_dag.py",
    ]:
        assert not (ROOT / relative).exists()
    assert (
        ROOT / "apps/data-platform/feature-store/feature_repo/feature_store.yaml"
    ).exists()


def test_required_operational_airflow_dags_are_restored_without_removed_dags():
    source = (
        ROOT
        / "apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py"
    ).read_text()

    for dag_id in ["recsys_feast_materialize", "recsys_feature_drift_monitoring"]:
        assert f'dag_id="{dag_id}"' in source
    assert "trigger_kubeflow_retrain_if_drift" in source
    assert "recsys_lakehouse_maintenance" not in source
    assert "k8s_data_platform_dag" not in source
    assert "recsys_batch_feature_pipeline" not in source
