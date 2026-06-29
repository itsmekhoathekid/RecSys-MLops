from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from jenkins.scripts.detect_changed_components import classify


def enabled(paths: list[str]) -> set[str]:
    flags = classify(paths)
    return {name.removeprefix("RUN_") for name, value in flags.items() if name.startswith("RUN_") and value}


def test_api_change_routes_only_api_component():
    assert enabled(["apps/api-serving/src/main.py"]) >= {"API", "COMPONENT_CI", "COMPONENT_BUILD", "COMPONENT_DEPLOY", "PYTHON"}
    assert "TRAINING" not in enabled(["apps/api-serving/src/main.py"])


def test_training_change_routes_training_and_model_promotion_routes_kserve():
    assert "TRAINING" in enabled(["apps/ml-system/src/kubeflow/pipelines/bst_training_pipeline.py"])
    assert {"TRAINING", "KSERVE"} <= enabled(["apps/ml-system/src/registry/model_promotion.py"])


def test_spark_batch_paths_route_spark_batch_dp2_dp3():
    components = enabled(["apps/data-platform/src/features/spark/spark_batch_entrypoint.py"])
    assert {"SPARK_BATCH", "DP2", "DP3"} <= components
    assert "API" not in components


def test_dp1_paths_route_raw_to_bronze_only():
    components = enabled(["apps/data-platform/src/ingest/batch_lakehouse_ingestion.py"])
    assert "DP1" in components
    assert "API" not in components


def test_streaming_paths_route_offline_and_online_stream_jobs():
    components = enabled(["apps/data-platform/src/features/flink/realtime_stream_job.py"])
    assert {"STREAM_OFFLINE", "STREAM_ONLINE"} <= components


def test_data_platform_chart_fans_out_to_data_components():
    components = enabled(["infra/helm/recsys-data-platform/templates/airflow.yaml"])
    assert {"MATERIALIZE", "SPARK_BATCH", "DP1", "DP2", "DP3", "DRIFT", "STREAM_OFFLINE", "STREAM_ONLINE"} <= components
    assert "API" not in components


def test_serving_chart_routes_api_and_kserve_only():
    components = enabled(["infra/helm/recsys-serving/templates/inferenceservice.yaml"])
    assert {"API", "KSERVE"} <= components
    assert "DP1" not in components
