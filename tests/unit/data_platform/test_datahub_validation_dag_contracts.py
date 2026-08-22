import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DATA_PLATFORM_DAGS = (
    "recsys_dp1_raw_to_bronze.py",
    "recsys_dp2_bronze_to_silver_gold.py",
    "recsys_dp3_offline_feature_table.py",
    "recsys_feast_materialize.py",
    "recsys_rag_item_index.py",
)


def _publisher_call(path: Path) -> ast.Call:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "publish_datahub_validation"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Call):
            return node.value
    raise AssertionError(f"publish_datahub_validation task not found in {path}")


def _literal_keyword(call: ast.Call, name: str):
    for keyword in call.keywords:
        if keyword.arg == name:
            return ast.literal_eval(keyword.value)
    raise AssertionError(f"Missing {name} keyword")


def test_datahub_publishers_wait_for_successful_report_producers_and_retry():
    dag_root = ROOT / "apps/data-platform/src/orchestration/airflow/dags"
    for filename in DATA_PLATFORM_DAGS:
        publisher = _publisher_call(dag_root / filename)
        assert _literal_keyword(publisher, "trigger_rule") == "all_success"
        assert _literal_keyword(publisher, "retries") == 2


def test_analytics_datahub_publisher_waits_for_successful_report_producer():
    dag_path = (
        ROOT / "apps/analytics/orchestration/airflow/dags/recsys_analytics_daily.py"
    )
    publisher = _publisher_call(dag_path)
    assert _literal_keyword(publisher, "trigger_rule") == "all_success"
    assert '"retries": 2' in dag_path.read_text(encoding="utf-8")
