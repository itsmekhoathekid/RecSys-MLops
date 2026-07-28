from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from jenkins.python.change_detection import detector as detect_changed_components
from jenkins.python.change_detection.detector import (
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
        ("infra/helm/recsys-ci/values.yaml", {"ROLLOUT"}, True),
        ("jenkins/jobs/recsys-cicd-proof-config.xml", set(), True),
        ("jenkins/scripts/entrypoints/component_ci.sh", set(COMPONENTS), True),
        ("apps/api-serving/src/main.py", {"API"}, False),
        ("apps/ml-system/src/training/train.py", {"TRAINING"}, False),
        ("apps/ml-system/src/cli/model_rollout_controller.py", {"ROLLOUT"}, False),
        ("apps/analytics/models/marts/recsys/mart_recsys_funnel_daily.sql", {"ANALYTICS"}, False),
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
        (
            "apps/data-platform/src/feature_store/feast_registry.py",
            {"MATERIALIZE", "API"},
            False,
        ),
        (
            "apps/data-platform/src/feature_store/sql_registry_state.py",
            {"MATERIALIZE", "API"},
            False,
        ),
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
        ("apps/data-platform/Dockerfile.data-ingestion", {"DP1"}, False),
        (
            "apps/data-platform/Dockerfile.feature-store",
            {"MATERIALIZE", "DP3"},
            False,
        ),
        ("apps/data-platform/Dockerfile.drift-retrain", {"DRIFT"}, False),
        (
            "apps/data-platform/data-generator/src/drift/reporting.py",
            {"DP1", "DRIFT"},
            False,
        ),
        ("tests/unit/data_generator/test_drift_reporting_unit.py", {"DP1", "DRIFT"}, False),
        ("configs/local/data_generator_drift.yaml", {"DP1", "DRIFT"}, False),
        (
            "apps/data-platform/src/mlops/trigger_kubeflow_retrain.py",
            {"TRAINING", "DRIFT"},
            False,
        ),
        (
            "apps/data-platform/feature-store/feature_repo/recsys_feature_definitions.py",
            {"MATERIALIZE", "DP3", "STREAM_OFFLINE", "STREAM_ONLINE", "API"},
            False,
        ),
        ("configs/local/spark_batch.yaml", {"SPARK_BATCH", "DP2", "DP3"}, False),
        ("configs/local/flink_streaming.yaml", {"STREAM_OFFLINE", "STREAM_ONLINE"}, False),
        (
            "infra/helm/recsys-data-platform/templates/airflow.yaml",
            {"MATERIALIZE", "SPARK_BATCH", "DP1", "DP2", "DP3", "DRIFT", "STREAM_OFFLINE", "STREAM_ONLINE"},
            False,
        ),
        ("infra/helm/recsys-serving/templates/inferenceservice.yaml", {"API", "KSERVE", "ROLLOUT"}, False),
        ("infra/helm/recsys-analytics/templates/trino.yaml", {"ANALYTICS"}, False),
        ("infra/helm/recsys-observability/values.yaml", {"API", "KSERVE", "ROLLOUT", "DRIFT"}, False),
        ("infra/k8s/processing-baseline/spark-baseline-ui-job.yaml", {"SPARK_BATCH", "STREAM_OFFLINE"}, False),
        ("notebooks/ml.ipynb", {"TRAINING"}, False),
        ("jenkins/KServeModelCD.Jenkinsfile", {"ROLLOUT"}, True),
        ("jenkins/python/model_cd/cli.py", {"KSERVE", "ROLLOUT"}, True),
        ("jenkins/scripts/test/champion_only.sh", {"ROLLOUT"}, True),
        ("jenkins/scripts/build/kfp_package.sh", {"TRAINING"}, True),
        ("tests/unit/jenkins/test_detect_changed_components.py", set(), True),
        ("tests/contract/test_gateway_contracts.py", {"API", "DEMO_WEB"}, False),
        ("tests/unit/api_serving/test_serving.py", {"API"}, False),
        ("tests/unit/ml_system/test_model_promotion.py", {"TRAINING", "KSERVE"}, False),
        ("tests/unit/ml_system/test_model_rollout_controller.py", {"TRAINING", "ROLLOUT"}, False),
        ("tests/unit/data_platform/test_lakehouse_optimization.py", {"SPARK_BATCH", "DP2", "DP3"}, False),
        ("tests/unit/test_runtime_lineage.py", {"MATERIALIZE", "DP1", "DP2", "DP3"}, False),
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
        "infra/helm/recsys-ci/templates/jenkins.yaml",
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
            raise subprocess.CalledProcessError(128, ["git", *args])
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
    paths = [
        path
        for path in subprocess.check_output(
            ["git", "ls-files"], cwd=ROOT, text=True
        ).splitlines()
        if (ROOT / path).exists()
    ]
    result = classify_paths(paths)

    assert result.unmapped_paths == ()


def test_hudi_native_upsert_integration_routes_to_batch_data_components():
    result = classify_paths(
        ["tests/integration/test_hudi_native_upsert_versioning.py"]
    )

    assert result.flags["RUN_SPARK_BATCH"]
    assert result.flags["RUN_DP1"]
    assert result.flags["RUN_DP2"]
    assert result.flags["RUN_DP3"]


def test_jenkinsfile_uses_previous_built_commit_and_has_ci_config_stage():
    source = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")
    helper = (ROOT / "jenkins/pipeline/component_pipeline.groovy").read_text(
        encoding="utf-8"
    )

    assert "env.GIT_PREVIOUS_COMMIT" in helper
    assert "componentPipeline.resolveChangedBaseRef()" in source
    assert "timeout 30s git fetch --no-tags origin" in source
    assert "stage('CI Configuration Validation')" in source
    assert "env.RUN_CI_CONFIG == 'true'" in source


def test_full_deploy_runs_demo_smoke_after_upstream_components():
    source = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")
    helper = (ROOT / "jenkins/pipeline/component_pipeline.groovy").read_text(
        encoding="utf-8"
    )
    deploy_helper = helper.split(
        "def runComponentDeployBranches(String scriptPath, String extraEnv) {", 1
    )[1].split("def applyForcedComponents", 1)[0]

    parallel_index = deploy_helper.index("parallel upstreamBranches")
    demo_index = deploy_helper.index("${scriptPath} demo_web")

    assert "component.name != 'demo_web'" in deploy_helper
    assert "env.RUN_DEMO_WEB == 'true'" in deploy_helper
    assert parallel_index < demo_index
    assert source.count(
        "runComponentDeployBranches('jenkins/scripts/entrypoints/component_deploy.sh', commandEnv)"
    ) == 2


def test_jenkins_seed_creates_post_promotion_kserve_cd_view():
    seed = (ROOT / "infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml").read_text(encoding="utf-8")
    model_cd_pipeline = (ROOT / "jenkins/KServeModelCD.Jenkinsfile").read_text(encoding="utf-8")

    assert "RecSys-KServe-Model-CD" in seed
    assert "06A KServe Model CD" in seed
    assert "CpsScmFlowDefinition" in seed
    assert "jenkins/KServeModelCD.Jenkinsfile" in seed
    assert "PROMOTION_MANIFEST_URI" in seed
    assert "RECSYS_CI_WORKSPACE" not in seed
    assert "AB_CANDIDATE_WEIGHT_PERCENT" in seed
    assert "AB_MIN_SAMPLES" in seed
    assert "CpsFlowDefinition" not in seed
    assert "stage('Checkout Rollout Source')" in model_cd_pipeline
    assert "checkout scm" in model_cd_pipeline


def test_progressive_rollout_cicd_is_wired_into_main_flow():
    jenkinsfile = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")
    components = (ROOT / "jenkins/config/components.json").read_text(encoding="utf-8")
    component_ci = (ROOT / "jenkins/scripts/ci/dispatch.sh").read_text(encoding="utf-8")
    component_build = (ROOT / "jenkins/scripts/build/dispatch.sh").read_text(encoding="utf-8")
    component_deploy = (ROOT / "jenkins/scripts/deploy/dispatch.sh").read_text(encoding="utf-8")
    rollout_deploy = (ROOT / "jenkins/scripts/deploy/rollout.sh").read_text(encoding="utf-8")
    seed = (ROOT / "infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml").read_text(
        encoding="utf-8"
    )

    result = classify_paths(["apps/ml-system/src/cli/model_rollout_controller.py"])

    assert result.component_names == ("rollout",)
    assert result.flags["RUN_ROLLOUT"] is True
    assert result.flags["RUN_COMPONENT_CI"] is True
    assert result.flags["RUN_COMPONENT_BUILD"] is True
    assert result.flags["RUN_COMPONENT_DEPLOY"] is True
    assert '"label": "Progressive Model Rollout"' in components
    assert "jenkins/pipeline/component_pipeline.groovy" in jenkinsfile
    assert "rollout)" in component_ci
    assert "rollout)" in component_build
    assert "rollout)" in component_deploy
    assert "deploy_rollout_watcher" in component_deploy
    assert "reconcile_rollout_jenkins_jobs" in rollout_deploy
    assert 'data.zz-seed-cicd-views\\.groovy' in rollout_deploy
    assert '"${jenkins_url}/scriptText"' in rollout_deploy
    assert 'view: "06B Progressive Model Rollout"' in seed
    assert 'job: "RecSys-Progressive-Rollout-CICD"' in seed
    assert 'component: "rollout"' in seed


def test_analytics_cicd_is_wired_from_main_detector_to_dedicated_view():
    jenkinsfile = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")
    components = (ROOT / "jenkins/config/components.json").read_text(encoding="utf-8")
    detector = (
        ROOT / "jenkins/python/change_detection/detector.py"
    ).read_text(encoding="utf-8")
    component_ci = (ROOT / "jenkins/scripts/ci/dispatch.sh").read_text(encoding="utf-8")
    component_build = (ROOT / "jenkins/scripts/build/dispatch.sh").read_text(encoding="utf-8")
    component_deploy = (ROOT / "jenkins/scripts/deploy/dispatch.sh").read_text(encoding="utf-8")
    seed = (ROOT / "infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml").read_text(
        encoding="utf-8"
    )

    result = classify_paths(
        [
            "apps/analytics/models/marts/recsys/mart_recsys_funnel_daily.sql",
            "infra/helm/recsys-analytics/templates/superset.yaml",
        ]
    )

    assert result.component_names == ("analytics",)
    assert result.flags["RUN_ANALYTICS"] is True
    assert result.flags["RUN_COMPONENT_CI"] is True
    assert result.flags["RUN_COMPONENT_BUILD"] is True
    assert result.flags["RUN_COMPONENT_DEPLOY"] is True
    assert '"label": "Analytics And BI"' in components
    assert "jenkins/pipeline/component_pipeline.groovy" in jenkinsfile
    assert 'normalized.startswith("apps/analytics/")' in detector
    assert 'path.startswith("infra/helm/recsys-analytics/")' in detector
    assert "analytics)" in component_ci
    assert "analytics)" in component_build
    assert "analytics)" in component_deploy
    assert 'view: "11 Analytics And BI"' in seed
    assert 'job: "RecSys-Analytics-BI-CICD"' in seed
    assert 'component: "analytics"' in seed


def test_jenkins_admin_secret_is_reconciled_with_persisted_home():
    init = (ROOT / "infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml").read_text(encoding="utf-8")
    security_script = init.split("basic-security.groovy: |", 1)[1].split(
        "seed-github-cicd-job.groovy: |", 1
    )[0]

    assert "HudsonPrivateSecurityRealm.Details.fromPlainPassword(password)" in security_script
    assert "admin.addProperty" in security_script
    assert "realm.getUser(username) == null" not in security_script


def test_component_ci_syncs_only_locked_profile_dependencies():
    jenkinsfile = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")
    installer = (ROOT / "jenkins/scripts/entrypoints/prepare_component_ci_envs.sh").read_text(
        encoding="utf-8"
    )
    component_ci = (
        (ROOT / "jenkins/scripts/entrypoints/component_ci.sh").read_text(encoding="utf-8")
        + (ROOT / "jenkins/scripts/ci/runtime.sh").read_text(encoding="utf-8")
    )
    python_env_stage = jenkinsfile.split("stage('Python Env')", 1)[1].split(
        "stage('Component CI')", 1
    )[0]

    assert "jenkins/scripts/entrypoints/prepare_component_ci_envs.sh" in jenkinsfile
    assert "uv pip install" not in python_env_stage
    assert "configuration.py ci-profiles" in installer
    assert "--frozen" in installer
    assert "--group dev" in installer
    assert "envs/${profile}" in installer
    assert '"${ci_python}" -m pytest' in component_ci
    assert 'PYSPARK_PYTHON="${ci_python}"' in component_ci
    assert 'PYSPARK_DRIVER_PYTHON="${ci_python}"' in component_ci
    assert "uv run --no-sync pytest" not in component_ci


def test_shell_deploy_gate_accepts_an_exact_origin_main_checkout():
    deploy = (ROOT / "jenkins/scripts/entrypoints/component_deploy.sh").read_text(encoding="utf-8")

    assert "git rev-parse --verify origin/main" in deploy
    assert '"$(git rev-parse HEAD)" == "$(git rev-parse origin/main)"' in deploy
    assert '[[ "${checked_out_main}" != "1" ]]' in deploy


def test_container_scans_are_serialized_and_share_the_trivy_database_cache():
    engine = (ROOT / "jenkins/scripts/build/engine.sh").read_text(encoding="utf-8")

    assert 'lock_path="${lock_root}/trivy-scan.lock"' in engine
    assert 'flock -w "${TRIVY_SCAN_LOCK_TIMEOUT_SECONDS:-3600}" 8' in engine
    assert 'scan_cache_volume="${TRIVY_CACHE_VOLUME:-recsys-trivy-cache}"' in engine
    assert (
        '--mount "type=volume,source=${scan_cache_volume},target=/root/.cache"'
        in engine
    )
    assert 'docker rm -f "${scan_container}"' in engine
    assert 'rm -f "${archive}"' in engine


def test_data_platform_deploy_preserves_isolated_drift_snapshot_root():
    deploy = (ROOT / "jenkins/scripts/deploy/data_platform.sh").read_text(encoding="utf-8")

    assert "drift.currentRoot=${OFFLINE_FEATURE_DRIFT_CURRENT_ROOT:-" in deploy
    assert "monitoring/offline_feature_drift/current_snapshot" in deploy


def test_rollout_job_reconciliation_uses_valid_python_crumb_parser():
    deploy = (ROOT / "jenkins/scripts/deploy/rollout.sh").read_text(encoding="utf-8")

    assert 'print("{}: {}".format(p["crumbRequestField"], p["crumb"]))' in deploy
    assert 'p[\\"crumbRequestField\\"]' not in deploy
    assert 'curl -fsS -c "${cookie_file}"' in deploy
    assert '-b "${cookie_file}"' in deploy


def test_jenkins_ci_temp_data_uses_persistent_storage_not_node_ephemeral_disk():
    jenkinsfile = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")

    assert 'env.CI_TMP_ROOT = "/var/jenkins_home/ci-tmp/' in jenkinsfile
    assert 'env.CI_TMP_ROOT = "/tmp/' not in jenkinsfile


def test_kfp_cicd_reuses_the_prepared_jenkins_python_environment():
    script = (ROOT / "jenkins/scripts/build/kfp_package.sh").read_text(encoding="utf-8")

    prepared_env_branch = script.index('UV_PROJECT_ENVIRONMENT:-')
    uv_fallback_branch = script.index('command -v uv')
    assert prepared_env_branch < uv_fallback_branch
    assert 'python_cmd=("${UV_PROJECT_ENVIRONMENT}/bin/python")' in script


def test_gke_l7_backend_stays_on_the_untainted_cpu_pool():
    rebalance = (ROOT / "infra/k8s/scripts/rebalance_ml_node_pool.sh").read_text(encoding="utf-8")
    validator = (ROOT / "jenkins/scripts/test/node_placement.sh").read_text(encoding="utf-8")

    assert "patch_gke_managed_deployment_cpu l7-default-backend" in rebalance
    ml_list = rebalance.split("kube_system_ml_deployments=(", 1)[1].split(")", 1)[0]
    assert "l7-default-backend" not in ml_list
    assert "assert_deployment_selector kube-system l7-default-backend" in validator
