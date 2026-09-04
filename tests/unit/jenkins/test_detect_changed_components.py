from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from jenkins.python.change_detection import detector  # noqa: E402
from jenkins.python.change_detection.detector import (  # noqa: E402
    ChangedFile,
    changed_files,
    detect_changed_components,
    render_jenkins_environment,
)
from jenkins.python.release_plan import create_release_plan  # noqa: E402


PIPELINE_RELEASE_PLAN_GOLDENS = {
    "rag": {
        "components": ["rag_index", "rag_api"],
        "buildImages": [
            "recsys-base-python",
            "recsys-datahub-ops",
            "recsys-rag-model-e5",
            "recsys-rag-indexer",
            "recsys-rag-admin",
            "recsys-airflow",
            "recsys-rag-api",
        ],
        "deployUnits": [
            "milvus",
            "milvus-credentials",
            "rag-feature-registry",
            "rag-api",
            "airflow",
        ],
    },
    "context": {
        "components": ["feature_rag_mcp", "context_agent"],
        "buildImages": ["recsys-feature-rag-mcp"],
        "deployUnits": [
            "feature-rag-mcp",
            "context-agent",
            "feature-rag-mcp-registry",
            "context-agent-registry",
        ],
    },
    "recommendation": {
        "components": ["recommendation_mcp", "recommendation_agent"],
        "buildImages": ["recsys-recommendation-mcp"],
        "deployUnits": [
            "recommendation-mcp",
            "recommendation-agent",
            "recommendation-mcp-registry",
            "recommendation-agent-registry",
        ],
    },
    "coordinator": {
        "components": [
            "feature_rag_mcp",
            "context_agent",
            "recommendation_mcp",
            "recommendation_agent",
            "coordinator_agent",
        ],
        "buildImages": ["recsys-feature-rag-mcp", "recsys-recommendation-mcp"],
        "deployUnits": [
            "feature-rag-mcp",
            "context-agent",
            "feature-rag-mcp-registry",
            "context-agent-registry",
            "recommendation-mcp",
            "recommendation-agent",
            "recommendation-mcp-registry",
            "recommendation-agent-registry",
            "coordinator-agent",
            "coordinator-agent-registry",
        ],
    },
}


@pytest.mark.parametrize("golden", PIPELINE_RELEASE_PLAN_GOLDENS.values())
def test_dedicated_pipeline_release_plan_golden(golden):
    plan = create_release_plan(golden["components"], commit="golden")
    assert plan["components"] == golden["components"]
    assert plan["buildImages"] == golden["buildImages"]
    assert plan["deployUnits"] == golden["deployUnits"]


def test_forced_pipeline_scope_ignores_unrelated_changed_chart_paths():
    result = detect_changed_components(
        [ChangedFile("M", "infra/helm/recsys-analytics/values.yaml")],
        forced_components="recommendation_mcp,recommendation_agent,ci_config",
        commit="forced",
    )

    assert list(result.component_names) == PIPELINE_RELEASE_PLAN_GOLDENS[
        "recommendation"
    ]["components"]
    assert result.release_plan["deployUnits"] == PIPELINE_RELEASE_PLAN_GOLDENS[
        "recommendation"
    ]["deployUnits"]


def test_coordinator_change_expands_release_dependencies():
    result = detect_changed_components(
        [ChangedFile("M", "configs/agentic/recsys-coordinator-agent/tools-contract.json")],
        commit="coordinator",
    )

    assert list(result.component_names) == PIPELINE_RELEASE_PLAN_GOLDENS[
        "coordinator"
    ]["components"]
    assert result.release_plan["deployUnits"] == PIPELINE_RELEASE_PLAN_GOLDENS[
        "coordinator"
    ]["deployUnits"]


def selected(paths: list[str]) -> set[str]:
    return set(
        detect_changed_components(
            [ChangedFile("M", path) for path in paths]
        ).component_names
    )


def detect(paths: list[str]):
    return detect_changed_components([ChangedFile("M", path) for path in paths])


def test_dp2_entrypoint_selects_only_dp2():
    result = detect(
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


def test_shared_data_platform_dag_helper_selects_all_consumers():
    assert selected(
        ["apps/data-platform/src/orchestration/airflow/spark_utils.py"]
    ) == {"materialize", "dp1", "dp2", "dp3", "rag_index", "drift"}


def test_metadata_change_selects_the_static_datahub_catalog_component():
    result = detect(["apps/data-platform/src/metadata/governance_catalog.py"])
    assert result.component_names == ("datahub_catalog",)
    assert "recsys-datahub-ops" in result.release_plan["buildImages"]
    assert "datahub-catalog" in result.release_plan["deployUnits"]


def test_each_split_airflow_dag_selects_only_its_component():
    expected = {
        "recsys_dp1_raw_to_bronze.py": "dp1",
        "recsys_dp2_bronze_to_silver_gold.py": "dp2",
        "recsys_dp3_offline_feature_table.py": "dp3",
        "recsys_feast_materialize.py": "materialize",
        "recsys_feature_drift_monitoring.py": "drift",
    }

    for filename, component in expected.items():
        path = f"apps/data-platform/src/orchestration/airflow/dags/{filename}"
        assert detect([path]).component_names == (component,)


def test_spark_runtime_dockerfile_expands_through_leaf_consumers():
    result = detect(["images/data/recsys-spark-runtime/Dockerfile"])
    assert set(result.component_names) == {
        "training",
        "dp1",
        "dp2",
        "dp3",
        "analytics",
    }
    assert result.changed_images == ("recsys-spark-runtime",)


def test_component_exclude_wins_over_broad_dp3_prefix():
    result = detect(
        ["apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py"]
    )
    assert "dp3" not in result.component_names


def test_docs_and_generated_files_are_ignored():
    result = detect(
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
    result = detect(["jenkins/config/deploy-units.json"])
    assert result.flags["RUN_CI_CONFIG"]
    assert result.component_names == ()
    assert "CHANGED_COMPONENTS=ci_config" not in render_jenkins_environment(result)


def test_agent_registry_llm_and_vault_configs_select_ci_configuration_only():
    for path in (
        "configs/agentregistry/values.yaml",
        "configs/kagent/values.yaml",
        "configs/llm-d/agentgateway-values.yaml",
        "configs/vault/values.yaml.tftpl",
    ):
        result = detect([path])
        assert result.flags["RUN_CI_CONFIG"] is True
        assert result.component_names == ()
        assert result.unmapped_paths == ()


def test_unknown_runtime_path_fails_closed(monkeypatch, capsys, tmp_path):
    result = detect(["new-runtime/worker.py"])
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
    assert "ERROR: Unmapped active runtime path" in capsys.readouterr().out


def test_dp2_release_plan_does_not_expand_shared_spark_into_training_artifacts():
    plan = create_release_plan(
        ["dp2"],
        changed_paths=[
            "apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py"
        ],
        commit="abc",
    )
    assert plan["buildImages"] == [
        "recsys-spark-runtime",
        "recsys-spark-data",
        "recsys-airflow",
    ]
    assert plan["buildArtifacts"] == []
    assert plan["deployUnits"] == ["airflow"]
    assert plan["version"] == 2
    assert "workflowChecks" not in plan


def test_training_release_plan_keeps_its_explicit_kubeflow_artifact():
    plan = create_release_plan(["training"], commit="abc")

    assert plan["buildArtifacts"] == ["kubeflow-bst"]
    assert "recsys-mlops-training" in plan["buildImages"]
    assert "recsys-spark-ml" in plan["buildImages"]
    assert "kubeflow-bst-package" in plan["deployUnits"]


def test_chart_change_selects_its_exact_deploy_unit():
    plan = create_release_plan(
        ["stream_online"],
        changed_paths=["infra/helm/recsys-event-stream/templates/kafka.yaml"],
    )
    assert "event-stream" in plan["deployUnits"]
    assert "data-lakehouse" not in plan["deployUnits"]


def test_feature_only_change_builds_and_deploys_only_feature_api():
    result = detect(
        ["apps/api-serving/online-feature-api/src/recsys_online_feature_api/app.py"]
    )

    assert result.component_names == ("online_feature_api",)
    assert result.release_plan["buildImages"] == ["recsys-online-feature-api"]
    assert result.release_plan["deployUnits"] == ["online-feature-api"]


def test_inference_only_change_builds_and_deploys_only_inference_api():
    result = detect(["apps/api-serving/inference-api/src/recsys_inference_api/app.py"])

    assert result.component_names == ("inference_api",)
    assert result.release_plan["buildImages"] == ["recsys-inference-api"]
    assert result.release_plan["deployUnits"] == ["inference-api"]


def test_shared_serving_contract_change_also_rebuilds_the_mcp_facade():
    result = detect(["apps/api-serving/shared/src/recsys_serving_common/contracts.py"])

    assert result.component_names == (
        "rag_api",
        "online_feature_api",
        "feature_rag_mcp",
        "inference_api",
    )
    assert result.release_plan["buildImages"] == [
        "recsys-online-feature-api",
        "recsys-inference-api",
        "recsys-rag-api",
        "recsys-feature-rag-mcp",
    ]
    assert result.release_plan["deployUnits"] == [
        "rag-api",
        "feature-rag-mcp",
        "feature-rag-mcp-registry",
        "online-feature-api",
        "inference-api",
    ]


def test_agentic_change_routing_and_release_order_matrix():
    mcp = detect(
        ["apps/agentic/recsys-feature-rag-mcp/src/recsys_feature_rag_mcp/app.py"]
    )
    assert mcp.component_names == ("feature_rag_mcp",)
    assert mcp.release_plan["buildImages"] == ["recsys-feature-rag-mcp"]
    assert mcp.release_plan["deployUnits"] == [
        "feature-rag-mcp",
        "feature-rag-mcp-registry",
    ]

    agent = detect(["infra/helm/recsys-kagent-agent/values.yaml"])
    assert list(agent.component_names) == PIPELINE_RELEASE_PLAN_GOLDENS["context"][
        "components"
    ]
    assert agent.release_plan["buildImages"] == PIPELINE_RELEASE_PLAN_GOLDENS[
        "context"
    ]["buildImages"]
    assert agent.release_plan["deployUnits"] == PIPELINE_RELEASE_PLAN_GOLDENS[
        "context"
    ]["deployUnits"]

    interface = detect(
        ["configs/agentic/recsys-context-agent/tools-contract.json"]
    )
    assert interface.component_names == ("feature_rag_mcp", "context_agent")
    assert interface.release_plan["buildImages"] == ["recsys-feature-rag-mcp"]
    assert interface.release_plan["deployUnits"] == [
        "feature-rag-mcp",
        "context-agent",
        "feature-rag-mcp-registry",
        "context-agent-registry",
    ]

    rag_contract = detect(
        ["apps/api-serving/rag-api/src/recsys_rag_api/contracts.py"]
    )
    assert rag_contract.component_names == ("rag_api", "feature_rag_mcp")
    units = rag_contract.release_plan["deployUnits"]
    assert units.index("rag-api") < units.index("feature-rag-mcp")


def test_coordinator_change_selects_its_helm_and_registry_units_in_order():
    coordinator = detect(
        ["infra/helm/recsys-coordinator-agent/templates/sandboxagent.yaml"]
    )
    assert list(coordinator.component_names) == PIPELINE_RELEASE_PLAN_GOLDENS[
        "coordinator"
    ]["components"]
    assert coordinator.release_plan["buildImages"] == PIPELINE_RELEASE_PLAN_GOLDENS[
        "coordinator"
    ]["buildImages"]
    assert coordinator.release_plan["deployUnits"] == PIPELINE_RELEASE_PLAN_GOLDENS[
        "coordinator"
    ]["deployUnits"]

    validation = detect(["ops/validation/coordinator_agentic_autoscale.sh"])
    assert list(validation.component_names) == PIPELINE_RELEASE_PLAN_GOLDENS[
        "coordinator"
    ]["components"]


def test_rag_change_detection_and_release_dependency_order():
    index = detect(["apps/data-platform/src/rag_data/semantic_chunker.py"])
    assert index.component_names == ("rag_index",)
    assert {"recsys-rag-indexer", "recsys-rag-admin", "recsys-airflow"}.issubset(
        index.release_plan["buildImages"]
    )

    api = detect(["apps/api-serving/rag-api/src/recsys_rag_api/app.py"])
    assert api.component_names == ("rag_api",)
    assert api.release_plan["buildImages"] == ["recsys-rag-api"]
    api_units = api.release_plan["deployUnits"]
    assert api_units == ["rag-api"]

    shared = selected(
        ["apps/data-platform/rag-runtime/src/recsys_rag_runtime/embedding.py"]
    )
    assert shared == {"rag_index", "rag_api"}

    feature_runtime = selected(
        [
            "apps/data-platform/feature-store/runtime/src/"
            "recsys_feature_store_runtime/feast_registry.py"
        ]
    )
    assert feature_runtime == {
        "dp3",
        "materialize",
        "training",
        "online_feature_api",
    }

    plan = create_release_plan(["rag_index", "rag_api"], commit="abc123")
    units = plan["deployUnits"]
    assert units.index("milvus") < units.index("milvus-credentials")
    assert units.index("milvus-credentials") < units.index("rag-feature-registry")
    assert units.index("rag-feature-registry") < units.index("rag-api")
    assert "rag-index-promotion" not in units


def test_data_dependent_actions_require_explicit_components():
    bootstrap = create_release_plan(
        ["dp1", "rag_api"],
        changed_images=["recsys-ingestion"],
    )
    assert "milvus" not in bootstrap["deployUnits"]
    assert "milvus-credentials" not in bootstrap["deployUnits"]
    assert "rag-feature-registry" not in bootstrap["deployUnits"]
    assert "datahub-catalog" not in bootstrap["deployUnits"]
    assert "rag-index-promotion" not in bootstrap["deployUnits"]

    data_ready = create_release_plan(["datahub_catalog", "rag_index"])
    assert "datahub-catalog" in data_ready["deployUnits"]
    assert "rag-index-promotion" not in data_ready["deployUnits"]


def test_kserve_only_change_builds_no_api_image():
    result = detect(["infra/helm/recsys-serving/templates/inferenceservice.yaml"])

    assert result.component_names == ("kserve",)
    assert result.release_plan["buildImages"] == []
    assert result.release_plan["deployUnits"] == ["kserve"]


def test_chart_only_change_deploys_exact_release_without_fake_component():
    result = detect(["infra/helm/recsys-data-config/values.yaml"])

    assert result.component_names == ()
    assert result.flags["RUN_CI_CONFIG"] is True
    assert result.flags["RUN_COMPONENT_CI"] is False
    assert result.flags["RUN_COMPONENT_BUILD"] is False
    assert result.flags["RUN_COMPONENT_DEPLOY"] is True


def test_gateway_chart_change_deploys_gateway_without_rebuilding_inference_api():
    result = detect(["infra/helm/recsys-gateway/templates/rag-ingress.yaml"])

    assert result.component_names == ()
    assert result.release_plan["buildImages"] == []
    assert result.release_plan["deployUnits"] == ["gateway"]
    assert result.flags["RUN_CI_CONFIG"] is True
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


def test_changed_files_preserves_successful_empty_diff(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_git_name_status(args: list[str]) -> list[ChangedFile]:
        calls.append(tuple(args))
        return []

    monkeypatch.setattr(detector, "_git_name_status", fake_git_name_status)
    assert changed_files("same") == []
    assert calls == [("diff", "--name-status", "-z", "same...HEAD")]


def test_changed_files_falls_back_to_current_commit(monkeypatch):
    def fake_git_name_status(args: list[str]) -> list[ChangedFile]:
        if args[0] == "diff":
            raise subprocess.CalledProcessError(128, ["git", *args])
        if args[0] == "diff-tree":
            return [ChangedFile("M", "apps/api-serving/shared/src/main.py")]
        return []

    monkeypatch.setattr(detector, "_git_name_status", fake_git_name_status)
    assert changed_files("missing") == [
        ChangedFile("M", "apps/api-serving/shared/src/main.py")
    ]


def test_deleted_unmapped_legacy_path_is_diagnostic_only():
    result = detect_changed_components([ChangedFile("D", "legacy/removed.sh")])
    assert result.unmapped_paths == ()
    assert result.deleted_unmapped_paths == ("legacy/removed.sh",)


def test_rename_is_classified_as_delete_and_add():
    changes = detector._parse_name_status(
        b"R100\0legacy/old.py\0apps/api-serving/shared/src/new.py\0"
    )
    assert changes == [
        ChangedFile("D", "legacy/old.py"),
        ChangedFile("A", "apps/api-serving/shared/src/new.py"),
    ]
    result = detect_changed_components(changes)
    assert result.component_names == (
        "rag_api",
        "online_feature_api",
        "inference_api",
    )
    assert result.deleted_unmapped_paths == ("legacy/old.py",)


def test_force_components_builds_one_plan_without_classifying_paths():
    result = detect_changed_components(
        [ChangedFile("M", "unmapped/ignored-by-force.py")],
        commit="abc",
        forced_components="dp2,ci_config",
    )
    assert result.component_names == ("dp2",)
    assert result.flags["RUN_CI_CONFIG"] is True
    assert result.unmapped_paths == ()
    assert result.release_plan["commit"] == "abc"


def test_detector_creates_release_plan_once(monkeypatch):
    original = detector.create_release_plan
    calls = 0

    def counted_create_release_plan(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(detector, "create_release_plan", counted_create_release_plan)
    result = detect(
        ["apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py"]
    )

    assert calls == 1
    assert result.release_plan["components"] == ["dp2"]


def test_detector_contains_no_domain_path_router_functions():
    source = (ROOT / "jenkins/python/change_detection/detector.py").read_text(
        encoding="utf-8"
    )
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
    result = detect(paths)
    assert result.unmapped_paths == ()
