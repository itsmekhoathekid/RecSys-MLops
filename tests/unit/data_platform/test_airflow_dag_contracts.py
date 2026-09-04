from pathlib import Path


DAGS = (
    "recsys_dp1_raw_to_bronze.py",
    "recsys_dp2_bronze_to_silver_gold.py",
    "recsys_dp3_offline_feature_table.py",
    "recsys_feast_materialize.py",
    "recsys_rag_item_index.py",
)


def test_data_platform_dags_have_no_datahub_runtime_coupling():
    root = Path("apps/data-platform/src/orchestration/airflow/dags")
    for filename in DAGS:
        source = (root / filename).read_text()
        assert "metadata.governance_catalog" not in source
        assert "in" + "lets=" not in source
        assert "out" + "lets=" not in source
        assert "publish_datahub_validation" in source
    analytics = Path(
        "apps/analytics/orchestration/airflow/dags/recsys_analytics_daily.py"
    ).read_text()
    assert "from metadata" not in analytics
    assert "in" + "lets=" not in analytics
    assert "out" + "lets=" not in analytics


def test_task_dependencies_and_rag_runtime_parameters_are_preserved():
    root = Path("apps/data-platform/src/orchestration/airflow/dags")
    assert (
        "ingest_stage >> optimize_stage >> validate_stage"
        in (root / DAGS[0]).read_text()
    )
    rag = (root / "recsys_rag_item_index.py").read_text()
    assert "RAG_ITEM_SOURCE_RUN_ID" in rag
    assert 'PIPELINE_RUN = "rag-{{ ts_nodash }}"' in rag
    assert '"30 2 * * *"' in rag
    assert 'tz="Asia/Ho_Chi_Minh"' in rag
    assert "resolve-source" in rag
    assert "verify-active-index" in rag and "rollback-index" in rag
    assert "semantic_chunk_items" in rag and "validate_and_publish_index" in rag
    assert '"mode": "incremental"' in rag
    assert "--mode '{{{{ params.mode }}}}'" in rag
    assert not (root / "recsys_rag_item_reconciliation.py").exists()


def test_operational_dag_commands_use_testable_runtime_modules():
    root = Path("apps/data-platform/src/orchestration/airflow/dags")
    drift = (root / "recsys_feature_drift_monitoring.py").read_text()
    feast = (root / "recsys_feast_materialize.py").read_text()

    assert "python -c" not in drift
    assert "python -m monitoring.push_drift_report_metrics" in drift
    assert '--pipeline-version-id "${KFP_PIPELINE_VERSION_ID:-}"' in drift
    assert "python -m feature_store.materialize_online" in feast
    assert "materialize-incremental $(date" not in feast
    assert "retries=2" in feast
