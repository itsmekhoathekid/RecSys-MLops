from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_airflow_task_pods_can_use_both_gke_node_pools():
    sources = (
        ROOT / "apps/data-platform/src/orchestration/airflow/spark_utils.py",
        ROOT / "apps/analytics/orchestration/airflow/dags/recsys_analytics_daily.py",
    )

    for source in sources:
        contents = source.read_text()
        assert 'NODE_SELECTOR", ""' in contents or '"",\n)' in contents
        assert 'key="recsys.ai/workload"' in contents
        assert 'value="ml-system"' in contents
        assert 'effect="NoSchedule"' in contents
        assert '"cpu": os.getenv("AIRFLOW_TASK_REQUEST_CPU", "50m")' in contents
        assert '"memory": os.getenv("AIRFLOW_TASK_REQUEST_MEMORY", "256Mi")' in contents
