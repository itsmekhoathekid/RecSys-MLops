from __future__ import annotations

import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
ANALYTICS = ROOT / "apps" / "analytics"
CHART = ROOT / "infra" / "helm" / "recsys-analytics"


def render_chart() -> str:
    return subprocess.check_output(
        ["helm", "template", "recsys-analytics", str(CHART)],
        cwd=ROOT,
        text=True,
    )


def test_dbt_project_and_contracts_cover_required_gold_marts():
    project = yaml.safe_load(
        (ANALYTICS / "dbt_project.yml").read_text(encoding="utf-8")
    )
    schema = yaml.safe_load(
        (ANALYTICS / "models" / "schema.yml").read_text(encoding="utf-8")
    )
    model_names = {item["name"] for item in schema["models"]}

    assert project["profile"] == "recsys_analytics"
    assert {
        "dim_product",
        "fct_order_items",
        "fct_recommendation_impressions",
        "mart_recsys_funnel_daily",
        "mart_ab_experiment_daily",
        "mart_product_performance_daily",
    }.issubset(model_names)


def test_ab_mart_never_fabricates_experiment_assignments():
    staging = (
        ANALYTICS / "models" / "staging" / "stg_recommendation_requests.sql"
    ).read_text()
    mart = (
        ANALYTICS / "models" / "marts" / "recsys" / "mart_ab_experiment_daily.sql"
    ).read_text()

    assert "json_extract_scalar" in staging
    assert "$.experiment_id" in staging
    assert "$.variant" in staging
    assert "experiment_id is not null" in mart
    assert "variant is not null" in mart


def test_helm_stack_uses_separate_catalog_and_superset_databases():
    rendered = render_chart()

    assert "name: recsys-analytics-catalog-postgres" in rendered
    assert "name: recsys-analytics-superset-postgres" in rendered
    assert 'image: "trinodb/trino:482"' in rendered
    assert "iceberg.catalog.type=jdbc" in rendered
    assert (
        "iceberg.jdbc-catalog.connection-user=${ENV:ANALYTICS_CATALOG_USER}" in rendered
    )
    assert "iceberg.jdbc-catalog.schema-version=V1" in rendered
    assert "fs.s3.enabled=true" in rendered
    assert "name: initialize-iceberg-jdbc-catalog" in rendered
    assert "local:///opt/recsys/apps/analytics/src/init_catalog.py" in rendered


def test_lakehouse_thrift_endpoint_exposes_all_iceberg_layers_internally():
    rendered = render_chart()

    assert "name: recsys-lakehouse-thrift" in rendered
    assert "type: ClusterIP" in rendered
    assert "containerPort: 10000" in rendered
    assert "org.apache.spark.sql.hive.thriftserver.HiveThriftServer2" in rendered
    assert "spark.sql.catalog.recsys.type=hadoop" in rendered
    assert "spark.sql.catalog.recsys_features.type=hadoop" in rendered
    assert "spark.sql.catalog.analytics.type=jdbc" in rendered
    assert "spark.sql.catalog.analytics.jdbc.password=\"$ANALYTICS_CATALOG_PASSWORD\"" in rendered
    assert "name: recsys-lakehouse-thrift-bootstrap" in rendered
    assert "/opt/spark/bin/beeline" in rendered
    assert "CREATE OR REPLACE GLOBAL TEMP VIEW bronze_orders" in rendered
    assert "CREATE OR REPLACE GLOBAL TEMP VIEW silver_product_scd" in rendered
    assert "CREATE OR REPLACE GLOBAL TEMP VIEW gold_ml_bst_training" in rendered
    assert "silver_order_facts" not in rendered

    deploy = (ROOT / "jenkins" / "scripts" / "deploy" / "analytics.sh").read_text()
    assert 'images.spark=$(resolve_release_image recsys-spark)' in deploy
    assert "wait_rollout_if_exists deployment recsys-lakehouse-thrift" in deploy


def test_superset_is_restricted_to_gold_schemas_through_trino_access_control():
    rendered = render_chart()

    assert (
        '"user": "superset", "catalog": "analytics", "allow": "read-only"' in rendered
    )
    assert '"schema": "(core|recsys)"' in rendered
    assert "--database_name RecSysAnalytics" in rendered
    assert "trino://superset@recsys-analytics-trino:8080/analytics/recsys" in rendered
    assert "redis://recsys-analytics-redis:6379/1" in rendered


def test_superset_dashboard_is_bootstrapped_idempotently_after_helm_upgrades():
    rendered = render_chart()

    assert "recsys-analytics-superset-dashboard-bootstrap" in rendered
    assert '"helm.sh/hook": post-install,post-upgrade' in rendered
    assert "/app/pythonpath/bootstrap_dashboards.py" in rendered

    dockerfile = (
        ROOT / "images/analytics/recsys-analytics-superset/Dockerfile"
    ).read_text()
    bootstrap = (ROOT / "apps/analytics/superset/bootstrap_dashboards.py").read_text()
    assert "COPY apps/analytics/superset/bootstrap_dashboards.py" in dockerfile
    assert 'DASHBOARD_SLUG = "recsys-business-pulse"' in bootstrap
    assert "RecSys Product Performance" in bootstrap


def test_airflow_dag_orders_silver_sync_before_dbt_build():
    dag = (
        ANALYTICS / "orchestration" / "airflow" / "dags" / "recsys_analytics_daily.py"
    ).read_text()
    airflow_image = (ROOT / "images/data/recsys-airflow/Dockerfile").read_text()

    assert "sync_silver >> dbt_build" in dag
    assert "recsys_analytics_daily" in dag
    assert '"retries": 2' in dag
    assert "--project-dir" in dag
    assert 'env_schedule("ANALYTICS_DAG_SCHEDULE", "30 2 * * *")' in dag
    assert "apps/analytics/orchestration/airflow/dags" in airflow_image


def test_airflow_pod_tasks_use_terminal_state_aware_cleanup():
    dag_files = (
        ANALYTICS / "orchestration" / "airflow" / "dags" / "recsys_analytics_daily.py",
        ROOT
        / "apps"
        / "data-platform"
        / "src"
        / "orchestration"
        / "airflow"
        / "spark_utils.py",
    )

    for dag_file in dag_files:
        contents = dag_file.read_text(encoding="utf-8")
        assert 'on_finish_action="delete_succeeded_pod"' in contents
        assert "is_delete_operator_pod" not in contents


def test_data_platform_airflow_dags_are_split_one_file_per_dag():
    dag_dir = (
        ROOT / "apps" / "data-platform" / "src" / "orchestration" / "airflow" / "dags"
    )
    expected = {
        "recsys_dp1_raw_to_bronze.py": "recsys_dp1_raw_to_bronze",
        "recsys_dp2_bronze_to_silver_gold.py": ("recsys_dp2_bronze_to_silver_gold"),
        "recsys_dp3_offline_feature_table.py": ("recsys_dp3_offline_feature_table"),
        "recsys_feast_materialize.py": "recsys_feast_materialize",
        "recsys_feature_drift_monitoring.py": ("recsys_feature_drift_monitoring"),
        "recsys_rag_item_index.py": "recsys_rag_item_index",
        "recsys_rag_item_reconciliation.py": "recsys_rag_item_reconciliation",
    }

    assert not (dag_dir / "k8s_data_platform_dag.py").exists()
    assert not (dag_dir / "rubric_data_pipeline_dags.py").exists()
    assert {path.name for path in dag_dir.glob("recsys_*.py")} == set(expected)
    for filename, dag_id in expected.items():
        contents = (dag_dir / filename).read_text(encoding="utf-8")
        assert f'dag_id="{dag_id}"' in contents
        assert contents.count("dag_id=") == 1


def test_drift_retrain_dag_fails_on_kfp_error_and_uses_mtls():
    dag = (
        ROOT
        / "apps"
        / "data-platform"
        / "src"
        / "orchestration"
        / "airflow"
        / "dags"
        / "recsys_feature_drift_monitoring.py"
    ).read_text(encoding="utf-8")

    assert "--fail-on-trigger-error" in dag
    assert "source_run_path=" not in dag
    assert dag.count("istio_inject=True") == 3
