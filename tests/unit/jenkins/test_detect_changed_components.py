from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from jenkins.python.change_detection import detector
from jenkins.python.change_detection.detector import (
    changed_paths,
    classify_paths,
    render_environment,
)
from jenkins.python.release_plan import create_release_plan


def selected(paths: list[str]) -> set[str]:
    return set(classify_paths(paths).component_names)


def test_dp2_entrypoint_selects_only_dp2():
    result = classify_paths(
        ["apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py"]
    )
    assert result.component_names == ("dp2",)
    assert result.flags["RUN_DP2"]
    assert not result.flags["RUN_DP1"]
    assert not result.flags["RUN_DP3"]


def test_shared_lakehouse_path_selects_all_declared_consumers():
    assert selected(["apps/data-platform/src/lakehouse/optimize.py"]) == {
        "dp1",
        "dp2",
        "dp3",
        "stream_offline",
    }


def test_one_path_can_match_multiple_components():
    assert selected(
        ["apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py"]
    ) == {"dp1", "dp2", "dp3"}


def test_spark_dockerfile_expands_through_image_catalog_consumers():
    result = classify_paths(["images/data/recsys-spark/Dockerfile"])
    assert set(result.component_names) == {
        "training",
        "dp1",
        "dp2",
        "dp3",
        "analytics",
    }
    assert result.changed_images == ("recsys-spark",)


def test_component_exclude_wins_over_broad_dp3_prefix():
    result = classify_paths(
        ["apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py"]
    )
    assert "dp3" not in result.component_names


def test_docs_and_generated_files_are_ignored():
    result = classify_paths(
        [
            "docs/architecture.md",
            "docs/submission/historical.md",
            "graphify-out/graph.json",
            "apps/data-platform/src/__pycache__/worker.pyc",
        ]
    )
    assert result.component_names == ()
    assert result.unmapped_paths == ()
    assert len(result.ignored_paths) == 4


def test_ci_configuration_path_does_not_fake_product_component():
    result = classify_paths(["jenkins/config/workflows.json"])
    assert result.flags["RUN_CI_CONFIG"]
    assert result.component_names == ()
    assert "CHANGED_COMPONENTS=ci_config" not in render_environment(result)


def test_unknown_runtime_path_fails_closed(monkeypatch, capsys, tmp_path):
    result = classify_paths(["new-runtime/worker.py"])
    assert result.unmapped_paths == ("new-runtime/worker.py",)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "detector",
            "--path",
            "new-runtime/worker.py",
            "--plan-output",
            str(tmp_path / "plan.json"),
        ],
    )
    assert detector.main() == 2
    assert "ERROR: Unmapped runtime path" in capsys.readouterr().out


def test_dp2_release_plan_builds_spark_and_immutable_airflow_once():
    plan = create_release_plan(
        ["dp2"],
        changed_paths=[
            "apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py"
        ],
        commit="abc",
    )
    assert plan["buildImages"] == ["recsys-spark", "recsys-airflow"]
    assert plan["buildArtifacts"] == ["kubeflow-bst"]
    assert plan["deployUnits"] == [
        "kubeflow-bst-package",
        "data-config",
        "airflow",
    ]
    assert plan["workflowChecks"] == ["recsys_dp2_bronze_to_silver_gold"]


def test_chart_change_selects_its_exact_deploy_unit():
    plan = create_release_plan(
        ["stream_online"],
        changed_paths=["infra/helm/recsys-event-stream/templates/kafka.yaml"],
    )
    assert "event-stream" in plan["deployUnits"]
    assert "data-lakehouse" not in plan["deployUnits"]


def test_chart_only_change_deploys_exact_release_without_fake_component():
    result = classify_paths(["infra/helm/recsys-data-config/values.yaml"])

    assert result.component_names == ()
    assert result.flags["RUN_CI_CONFIG"] is True
    assert result.flags["RUN_COMPONENT_CI"] is False
    assert result.flags["RUN_COMPONENT_BUILD"] is False
    assert result.flags["RUN_COMPONENT_DEPLOY"] is True


def test_dp1_plan_bootstraps_image_less_lakehouse_and_event_stream_releases():
    plan = create_release_plan(["dp1"])

    assert "data-lakehouse" in plan["deployUnits"]
    assert "event-stream" in plan["deployUnits"]


def test_detector_cli_writes_environment_and_plan(monkeypatch, tmp_path, capsys):
    plan_path = tmp_path / "plan.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "detector",
            "--path",
            "apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py",
            "--commit",
            "abc",
            "--plan-output",
            str(plan_path),
        ],
    )
    assert detector.main() == 0
    output = capsys.readouterr().out
    assert "RUN_DP2=true" in output
    assert "CHANGED_COMPONENTS=dp2" in output
    assert json.loads(plan_path.read_text())["commit"] == "abc"


def test_changed_paths_preserves_successful_empty_diff(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_git_lines(args: list[str]) -> list[str]:
        calls.append(tuple(args))
        return []

    monkeypatch.setattr(detector, "git_lines", fake_git_lines)
    assert changed_paths("same") == []
    assert calls == [("diff", "--name-only", "same...HEAD")]


def test_changed_paths_falls_back_to_current_commit(monkeypatch):
    def fake_git_lines(args: list[str]) -> list[str]:
        if args[0] == "diff":
            raise subprocess.CalledProcessError(128, ["git", *args])
        if args[0] == "diff-tree":
            return ["apps/api-serving/src/main.py"]
        return []

    monkeypatch.setattr(detector, "git_lines", fake_git_lines)
    assert changed_paths("missing") == ["apps/api-serving/src/main.py"]


def test_detector_contains_no_domain_path_router_functions():
    source = (
        ROOT / "jenkins/python/change_detection/detector.py"
    ).read_text(encoding="utf-8")
    assert "classify_data_platform_source" not in source
    assert "classify_airflow_dag" not in source
    assert "classify_tests" not in source
    assert "apply_path_rules" not in source


def test_every_tracked_runtime_path_is_mapped_or_ignored():
    paths = [
        path
        for path in subprocess.check_output(
            ["git", "ls-files"], cwd=ROOT, text=True
        ).splitlines()
        if (ROOT / path).exists()
    ]
    result = classify_paths(paths)
    assert result.unmapped_paths == ()
