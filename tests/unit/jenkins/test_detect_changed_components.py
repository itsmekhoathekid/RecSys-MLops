from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from jenkins.scripts import detect_changed_components
from jenkins.scripts.detect_changed_components import (
    COMPONENTS,
    changed_paths,
    classify_paths,
    render_environment,
)


def product_components(paths: list[str]) -> set[str]:
    result = classify_paths(paths)
    return {component for component in COMPONENTS if result.flags[f"RUN_{component}"]}


@pytest.mark.parametrize(
    ("path", "expected", "ci_config"),
    [
        ("docs/submission/rubic-(mini-coursework)/data_storage.md", set(), False),
        ("README.md", set(), False),
        ("graphify-out/graph.json", set(), False),
        ("Jenkinsfile", set(), True),
        ("infra/helm/recsys-ci/values.yaml", set(), True),
        ("jenkins/jobs/recsys-cicd-proof-config.xml", set(), True),
        ("jenkins/scripts/component_ci.sh", set(), True),
        ("apps/api-serving/src/main.py", {"API"}, False),
        ("apps/ml-system/src/training/train.py", {"TRAINING"}, False),
        ("apps/ml-system/src/registry/model_promotion.py", {"TRAINING", "KSERVE"}, False),
        (
            "apps/data-platform/src/features/spark/build_user_sequence_features.py",
            {"SPARK_BATCH", "DP3"},
            False,
        ),
        (
            "apps/data-platform/src/features/spark/build_silver_tables.py",
            {"SPARK_BATCH", "DP2", "DP3"},
            False,
        ),
        ("apps/data-platform/src/ingest/batch_lakehouse_ingestion.py", {"DP1"}, False),
        (
            "apps/data-platform/src/features/flink/realtime_stream_job.py",
            {"STREAM_OFFLINE", "STREAM_ONLINE"},
            False,
        ),
        ("apps/data-platform/src/feature_store/feast_registry.py", {"MATERIALIZE"}, False),
        ("apps/data-platform/src/feature_store/online_writer.py", {"STREAM_ONLINE"}, False),
        (
            "apps/data-platform/src/feature_store/postgres_offline_store.py",
            {"DP3", "STREAM_OFFLINE"},
            False,
        ),
        (
            "apps/data-platform/src/lakehouse/optimize.py",
            {"SPARK_BATCH", "DP1", "DP2", "DP3", "STREAM_OFFLINE"},
            False,
        ),
        (
            "apps/data-platform/src/metadata/ingest_datahub_governance.py",
            {"MATERIALIZE", "DP1", "DP2", "DP3"},
            False,
        ),
        ("apps/data-platform/src/validate/offline_feature_drift.py", {"DRIFT"}, False),
        (
            "apps/data-platform/src/mlops/trigger_kubeflow_retrain.py",
            {"TRAINING", "DRIFT"},
            False,
        ),
        (
            "apps/data-platform/feature-store/feature_repo/features.py",
            {"MATERIALIZE", "DP3", "STREAM_OFFLINE", "STREAM_ONLINE"},
            False,
        ),
        ("configs/local/spark_batch.yaml", {"SPARK_BATCH", "DP2", "DP3"}, False),
        ("configs/local/flink_streaming.yaml", {"STREAM_OFFLINE", "STREAM_ONLINE"}, False),
        (
            "infra/helm/recsys-data-platform/templates/airflow.yaml",
            {"MATERIALIZE", "SPARK_BATCH", "DP1", "DP2", "DP3", "DRIFT", "STREAM_OFFLINE", "STREAM_ONLINE"},
            False,
        ),
        ("infra/helm/recsys-serving/templates/inferenceservice.yaml", {"API", "KSERVE"}, False),
        ("infra/helm/recsys-observability/values.yaml", {"API", "KSERVE", "DRIFT"}, False),
        ("infra/k8s/processing-baseline/spark-baseline-ui-job.yaml", {"SPARK_BATCH", "STREAM_OFFLINE"}, False),
        ("notebooks/ml.ipynb", {"TRAINING"}, False),
        ("jenkins/KServeModelCD.Jenkinsfile", {"KSERVE"}, True),
        ("jenkins/scripts/kubeflow_pipeline_cicd.sh", {"TRAINING"}, True),
        ("jenkins/scripts/validation_load_test.sh", {"API"}, True),
        ("tests/unit/jenkins/test_detect_changed_components.py", set(), True),
        ("tests/unit/api_serving/test_serving.py", {"API"}, False),
        ("tests/unit/ml_system/test_model_promotion.py", {"TRAINING", "KSERVE"}, False),
        ("tests/unit/data_platform/test_lakehouse_optimization.py", {"SPARK_BATCH", "DP2", "DP3"}, False),
    ],
)
def test_path_classification_matrix(path, expected, ci_config):
    result = classify_paths([path])

    assert product_components([path]) == expected
    assert result.flags["RUN_CI_CONFIG"] is ci_config
    assert result.unmapped_paths == ()
    has_product = bool(expected)
    assert result.flags["RUN_COMPONENT_CI"] is has_product
    assert result.flags["RUN_COMPONENT_BUILD"] is has_product
    assert result.flags["RUN_COMPONENT_DEPLOY"] is has_product
    assert result.flags["RUN_PYTHON"] is has_product


def test_root_dependency_change_intentionally_routes_all_python_components():
    result = classify_paths(["pyproject.toml"])

    assert product_components(["pyproject.toml"]) == set(COMPONENTS)
    assert result.flags["RUN_CI_CONFIG"] is False


def test_docs_and_ci_config_regression_does_not_route_product_components():
    paths = [
        "docs/submission/rubic-(mini-coursework)/data_storage.md",
        "infra/helm/recsys-ci/values.yaml",
        "jenkins/jobs/recsys-cicd-proof-config.xml",
    ]
    result = classify_paths(paths)

    assert product_components(paths) == set()
    assert result.flags["RUN_CI_CONFIG"] is True
    assert result.ignored_paths == ("docs/submission/rubic-(mini-coursework)/data_storage.md",)
    assert result.component_names == ("ci_config",)
    assert "CHANGED_COMPONENTS=ci_config" in render_environment(result)


def test_docs_only_is_unchanged_and_skips_all_ci_cd_stages():
    result = classify_paths(["docs/architecture.md", "docs/pngs/diagram.png"])

    assert result.component_names == ()
    assert result.unmapped_paths == ()
    assert all(not value for value in result.flags.values())
    assert "CHANGED_COMPONENTS=unchanged" in render_environment(result)
    assert "IGNORED_PATHS_COUNT=2" in render_environment(result)


def test_multiple_paths_union_only_their_relevant_components():
    paths = [
        "apps/api-serving/src/ranking.py",
        "apps/data-platform/src/ingest/debezium.py",
        "apps/api-serving/src/api_runtime.py",
        "docs/notes.md",
    ]
    result = classify_paths(paths)

    assert product_components(paths) == {"API", "DP1"}
    assert result.unmapped_paths == ()
    assert result.component_names == ("dp1", "api")


def test_repeated_paths_for_same_component_are_not_reported_unmapped():
    result = classify_paths(
        [
            "apps/api-serving/src/ranking.py",
            "apps/api-serving/src/api_runtime.py",
        ]
    )

    assert product_components(list(result.changed_paths)) == {"API"}
    assert result.unmapped_paths == ()


def test_unknown_runtime_path_fails_closed_instead_of_running_everything(monkeypatch, capsys):
    result = classify_paths(["new-runtime/worker.py"])
    assert result.unmapped_paths == ("new-runtime/worker.py",)
    assert product_components(["new-runtime/worker.py"]) == set()

    monkeypatch.setattr(sys, "argv", ["detect_changed_components", "--path", "new-runtime/worker.py"])
    assert detect_changed_components.main() == 2
    assert "ERROR: Unmapped runtime path" in capsys.readouterr().out


def test_changed_paths_preserves_a_valid_empty_diff(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_git_lines(args: list[str]) -> list[str]:
        calls.append(tuple(args))
        return []

    monkeypatch.setattr(detect_changed_components, "git_lines", fake_git_lines)

    assert changed_paths("same-commit") == []
    assert calls == [("diff", "--name-only", "same-commit...HEAD")]


def test_changed_paths_falls_back_to_current_commit_when_git_history_is_unavailable(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_git_lines(args: list[str]) -> list[str]:
        calls.append(tuple(args))
        if args[0] == "diff":
            raise detect_changed_components.subprocess.CalledProcessError(128, ["git", *args])
        if args[0] == "diff-tree":
            return ["docs/submission/rubic-final-coursework-(final-ml)/routing_gateway.md"]
        return []

    monkeypatch.setattr(detect_changed_components, "git_lines", fake_git_lines)

    assert changed_paths("missing-base") == ["docs/submission/rubic-final-coursework-(final-ml)/routing_gateway.md"]
    assert calls[:2] == [
        ("diff", "--name-only", "missing-base...HEAD"),
        ("diff", "--name-only", "HEAD~1", "HEAD"),
    ]


def test_every_tracked_repo_path_is_explicitly_classified_or_ignored():
    paths = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    result = classify_paths(paths)

    assert result.unmapped_paths == ()


def test_jenkinsfile_uses_previous_built_commit_and_has_ci_config_stage():
    source = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")

    assert "env.GIT_PREVIOUS_COMMIT" in source
    assert "resolveChangedBaseRef()" in source
    assert "stage('CI Configuration Validation')" in source
    assert "env.RUN_CI_CONFIG == 'true'" in source


def test_jenkins_seed_creates_post_promotion_kserve_cd_view():
    seed = (ROOT / "infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml").read_text(encoding="utf-8")

    assert "RecSys-KServe-Model-CD" in seed
    assert "06A KServe Model CD" in seed
    assert "CpsFlowDefinition" in seed
    assert "PROMOTION_MANIFEST_URI" in seed
    assert "RECSYS_CI_WORKSPACE" in seed
    assert "component_deploy.sh kserve_model_cd" in seed
    assert "stage('Python Env')" not in seed
    assert "stage('Checkout')" not in seed
    assert "checkout scm" not in seed


def test_jenkins_admin_secret_is_reconciled_with_persisted_home():
    init = (ROOT / "infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml").read_text(encoding="utf-8")
    security_script = init.split("basic-security.groovy: |", 1)[1].split(
        "seed-github-cicd-job.groovy: |", 1
    )[0]

    assert "HudsonPrivateSecurityRealm.Details.fromPlainPassword(password)" in security_script
    assert "admin.addProperty" in security_script
    assert "realm.getUser(username) == null" not in security_script


def test_component_ci_installs_required_clean_environment_dependencies():
    jenkinsfile = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")
    installer = (ROOT / "jenkins/scripts/install_component_ci_dependencies.sh").read_text(
        encoding="utf-8"
    )

    assert "hypothesis" in jenkinsfile
    assert "jenkins/scripts/install_component_ci_dependencies.sh" in jenkinsfile
    assert "training" in installer
    assert "kserve" in installer
    assert "https://download.pytorch.org/whl/cpu" in installer
    assert '"ray[default,train,tune]"' in installer
    assert "mlflow" in installer


def test_jenkins_ci_temp_data_uses_persistent_storage_not_node_ephemeral_disk():
    jenkinsfile = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")

    assert 'env.CI_TMP_ROOT = "/var/jenkins_home/ci-tmp/' in jenkinsfile
    assert 'env.CI_TMP_ROOT = "/tmp/' not in jenkinsfile
