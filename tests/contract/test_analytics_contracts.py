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
    project = yaml.safe_load((ANALYTICS / "dbt_project.yml").read_text(encoding="utf-8"))
    schema = yaml.safe_load((ANALYTICS / "models" / "schema.yml").read_text(encoding="utf-8"))
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
    staging = (ANALYTICS / "models" / "staging" / "stg_recommendation_requests.sql").read_text()
    mart = (ANALYTICS / "models" / "marts" / "recsys" / "mart_ab_experiment_daily.sql").read_text()

    assert "json_extract_scalar" in staging
    assert "$.experiment_id" in staging
    assert "$.variant" in staging
    assert "experiment_id is not null" in mart
    assert "variant is not null" in mart


def test_helm_stack_uses_separate_catalog_and_superset_databases():
    rendered = render_chart()

    assert "name: recsys-analytics-catalog-postgres" in rendered
    assert "name: recsys-analytics-superset-postgres" in rendered
    assert "image: \"trinodb/trino:482\"" in rendered
    assert "iceberg.catalog.type=jdbc" in rendered
    assert "iceberg.jdbc-catalog.connection-user=${ENV:ANALYTICS_CATALOG_USER}" in rendered
    assert "iceberg.jdbc-catalog.schema-version=V1" in rendered
    assert "fs.s3.enabled=true" in rendered
    assert "name: initialize-iceberg-jdbc-catalog" in rendered
    assert "local:///opt/recsys/apps/analytics/src/init_catalog.py" in rendered


def test_superset_is_restricted_to_gold_schemas_through_trino_access_control():
    rendered = render_chart()

    assert '\"user\": \"superset\", \"catalog\": \"analytics\", \"allow\": \"read-only\"' in rendered
    assert '\"schema\": \"(core|recsys)\"' in rendered
    assert "--database_name RecSysAnalytics" in rendered
    assert "trino://superset@recsys-analytics-trino:8080/analytics/recsys" in rendered
    assert "redis://recsys-analytics-redis:6379/1" in rendered


def test_superset_dashboard_is_bootstrapped_idempotently_after_helm_upgrades():
    rendered = render_chart()

    assert "recsys-analytics-superset-dashboard-bootstrap" in rendered
    assert '"helm.sh/hook": post-install,post-upgrade' in rendered
    assert "/app/pythonpath/bootstrap_dashboards.py" in rendered

    dockerfile = (ROOT / "apps/analytics/Dockerfile.superset").read_text()
    bootstrap = (ROOT / "apps/analytics/superset/bootstrap_dashboards.py").read_text()
    assert "COPY apps/analytics/superset/bootstrap_dashboards.py" in dockerfile
    assert 'DASHBOARD_SLUG = "recsys-business-pulse"' in bootstrap
    assert "RecSys Product Performance" in bootstrap


def test_airflow_dag_orders_silver_sync_before_dbt_build():
    dag = (ANALYTICS / "orchestration" / "airflow" / "dags" / "analytics_dag.py").read_text()
    airflow_image = (ROOT / "infra" / "docker" / "Dockerfile.airflow").read_text()

    assert "sync_silver >> dbt_build" in dag
    assert "recsys_analytics_daily" in dag
    assert "apps/analytics/orchestration/airflow/dags" not in airflow_image
