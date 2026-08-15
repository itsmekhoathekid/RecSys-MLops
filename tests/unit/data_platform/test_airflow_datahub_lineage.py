from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

from metadata.governance_catalog import (
    BRONZE_URNS,
    ICEBERG_FEATURE_URNS,
    POSTGRES_FEATURE_URNS,
    REDIS_FEATURE_URNS,
    SILVER_URNS,
    dataset_urn,
)


def _urns(iolets) -> set[str]:
    return {item.urn for item in iolets}


def _dag(module_name: str, variable: str):
    module = importlib.import_module(module_name)
    return getattr(module, variable)


def test_dp1_dp2_dp3_tasks_declare_canonical_datahub_lineage():
    dp1 = _dag(
        "orchestration.airflow.dags.recsys_dp1_raw_to_bronze",
        "recsys_dp1_raw_to_bronze",
    )
    assert _urns(dp1.task_dict["ingest_stage"].outlets) == set(BRONZE_URNS.values())
    assert _urns(dp1.task_dict["optimize_stage"].inlets) == set(BRONZE_URNS.values())
    assert _urns(dp1.task_dict["optimize_stage"].outlets) == set(BRONZE_URNS.values())
    assert _urns(dp1.task_dict["validate_stage"].inlets) == set(BRONZE_URNS.values())

    dp2 = _dag(
        "orchestration.airflow.dags.recsys_dp2_bronze_to_silver_gold",
        "recsys_dp2_bronze_to_silver_gold",
    )
    assert _urns(dp2.task_dict["ingest_stage"].inlets) == set(BRONZE_URNS.values())
    assert _urns(dp2.task_dict["ingest_stage"].outlets) == set(SILVER_URNS.values())
    assert _urns(dp2.task_dict["optimize_stage"].inlets) == set(SILVER_URNS.values())
    assert _urns(dp2.task_dict["validate_stage"].inlets) == set(SILVER_URNS.values())

    dp3 = _dag(
        "orchestration.airflow.dags.recsys_dp3_offline_feature_table",
        "recsys_dp3_offline_feature_table",
    )
    assert _urns(dp3.task_dict["ingest_stage"].inlets) == set(SILVER_URNS.values())
    assert _urns(dp3.task_dict["ingest_stage"].outlets) == set(
        ICEBERG_FEATURE_URNS.values()
    ) | set(POSTGRES_FEATURE_URNS.values())
    assert _urns(dp3.task_dict["validate_stage"].inlets) == set(
        POSTGRES_FEATURE_URNS.values()
    )


def test_feast_and_analytics_tasks_declare_lineage_while_drift_has_none():
    feast = _dag(
        "orchestration.airflow.dags.recsys_feast_materialize",
        "recsys_feast_materialize",
    )
    materialize = feast.task_dict["feast_materialize_incremental"]
    assert _urns(materialize.inlets) == set(POSTGRES_FEATURE_URNS.values())
    assert _urns(materialize.outlets) == set(REDIS_FEATURE_URNS.values())

    drift = _dag(
        "orchestration.airflow.dags.recsys_feature_drift_monitoring",
        "recsys_feature_drift_monitoring",
    )
    assert all(not task.inlets and not task.outlets for task in drift.tasks)

    path = Path("apps/analytics/orchestration/airflow/dags/recsys_analytics_daily.py")
    spec = importlib.util.spec_from_file_location("recsys_analytics_daily_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sync = module.recsys_analytics_daily.task_dict["sync_silver_catalog"]
    expected_inputs = {
        *(SILVER_URNS[name] for name in module.SILVER_SOURCE_TABLES),
        *(BRONZE_URNS[name] for name in module.BRONZE_SOURCE_TABLES),
    }
    assert _urns(sync.inlets) == expected_inputs
    assert _urns(sync.outlets) == {
        dataset_urn("iceberg", f"analytics.staging.{name}")
        for name in module.SILVER_SOURCE_TABLES + module.BRONZE_SOURCE_TABLES
    }
    env_vars = {item.name: item.value for item in sync.env_vars}
    assert env_vars["RUNTIME_LINEAGE_ENABLED"] == "false"


def test_dp3_declared_lineage_follows_source_and_export_flags(monkeypatch):
    monkeypatch.setenv("DP3_SOURCE", "bronze_lakehouse")
    monkeypatch.setenv("FEAST_POSTGRES_EXPORT_ENABLED", "false")
    path = Path(
        "apps/data-platform/src/orchestration/airflow/dags/"
        "recsys_dp3_offline_feature_table.py"
    )
    spec = importlib.util.spec_from_file_location("recsys_dp3_bronze_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    ingest = module.recsys_dp3_offline_feature_table.task_dict["ingest_stage"]
    validate = module.recsys_dp3_offline_feature_table.task_dict["validate_stage"]
    assert _urns(ingest.inlets) == set(BRONZE_URNS.values())
    assert _urns(ingest.outlets) == set(ICEBERG_FEATURE_URNS.values())
    assert _urns(validate.inlets) == set(POSTGRES_FEATURE_URNS.values())


def test_airflow_pods_disable_the_direct_sdk_recorder():
    from orchestration.airflow.spark_utils import COMMON_ENV

    assert COMMON_ENV["RUNTIME_LINEAGE_ENABLED"] == "false"
