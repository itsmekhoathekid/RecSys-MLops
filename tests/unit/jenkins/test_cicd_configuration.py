from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONFIGURATION_PATH = ROOT / "jenkins/python/configuration.py"
SPEC = importlib.util.spec_from_file_location("jenkins_configuration", CONFIGURATION_PATH)
assert SPEC and SPEC.loader
configuration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configuration)

EXPECTED_STAGES = [
    "Checkout",
    "Detect Changed Components",
    "CI Configuration Validation",
    "Python Env",
    "Component CI",
    "Docker Login",
    "Component Build And Publish",
    "Component Deploy Or Update",
]
EXPECTED_LABELS = [
    "Materialize Pipeline",
    "Training Pipeline",
    "Spark Batch Processing",
    "DP1 Raw To Bronze",
    "DP2 Bronze To Silver Gold",
    "DP3 Offline Feature Table",
    "FastAPI Web API",
    "KServe Inference Engine",
    "Progressive Model Rollout",
    "Realtime Drift Detection",
    "Stream Features To Offline Store",
    "Stream Features To Online Store",
    "Analytics And BI",
    "Recommendation Demo Web",
]


def test_component_catalog_is_valid_and_preserves_stage_view_labels():
    components = configuration.load_components()
    assert [component["label"] for component in components] == EXPECTED_LABELS
    assert components[-1]["name"] == "demo_web"
    assert components[-1]["deployOrder"] > max(
        component["deployOrder"] for component in components[:-1]
    )


def test_root_jenkins_stage_view_contract_is_unchanged():
    source = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")
    assert re.findall(r"^\s*stage\('([^']+)'\)", source, flags=re.MULTILINE) == EXPECTED_STAGES
    assert "skipDefaultCheckout(true)" in source
    assert "script: 'git rev-parse HEAD'" in source
    assert "values_args=(" not in source
    assert "source jenkins/scripts/" not in source
    assert "sh '''#!/usr/bin/env bash" in source
    assert ". jenkins/scripts/deploy/preflight/gcp.sh" in source
    assert "jenkins/scripts/lib/gcp.sh" not in source
    cleanup = source.index('rm -rf "${CI_TMP_ROOT}"')
    recovery_exit = source.index('exit "${recovery_status}"')
    assert cleanup < recovery_exit


def test_gcp_production_target_is_strict_and_self_consistent():
    target = configuration.load_gcp_production()
    assert target == {
        "projectId": "rec-sys-503309",
        "region": "asia-southeast1",
        "zone": "asia-southeast1-b",
        "cluster": "recsys-mlops-gke",
        "context": "gke_rec-sys-503309_asia-southeast1-b_recsys-mlops-gke",
        "imageRegistry": "asia-southeast1-docker.pkg.dev/rec-sys-503309/recsys",
    }


def test_component_ci_profiles_use_repo_locks():
    profiles = configuration.load_ci_environments()
    components = {
        component["name"]: component for component in configuration.load_components()
    }
    assert set(profiles) == {"data", "ml", "serving", "demo", "analytics"}
    assert components["api"]["ciProfile"] == "serving"
    assert components["kserve"]["ciProfile"] == "ml"
    for profile in profiles.values():
        assert profile["lockFile"].endswith("/uv.lock")
        assert (ROOT / profile["lockFile"]).is_file()
        assert (ROOT / profile["projectPath"] / "pyproject.toml").is_file()


def test_model_cd_ci_coverage_targets_the_split_package_modules():
    expected_modules = {
        "jenkins.python.model_cd.cli",
        "jenkins.python.model_cd.config",
        "jenkins.python.model_cd.helm_release",
        "jenkins.python.model_cd.manifests",
        "jenkins.python.model_cd.promotion_gates",
    }
    for relative_path in ("jenkins/scripts/ci/ml.sh", "jenkins/scripts/ci/serving.sh"):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "cov_paths=(model_cd)" not in source
        assert "jenkins/scripts:apps/" not in source
        assert all(module in source for module in expected_modules)


def test_external_ci_audits_use_bounded_retry():
    completed = subprocess.run(
        [
            "bash",
            "-c",
            (
                "source jenkins/scripts/lib/common.sh; "
                "attempts=0; "
                "flaky() { attempts=$((attempts + 1)); [[ $attempts -ge 3 ]]; }; "
                "recsys_retry 3 0 flaky; "
                'printf "%s" "$attempts"'
            ),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.endswith("3")
    demo_ci = (ROOT / "jenkins/scripts/ci/demo.sh").read_text(encoding="utf-8")
    assert demo_ci.count("CI_AUDIT_MAX_ATTEMPTS") == 3


def test_catalog_contains_only_supported_migration_policies():
    payload = json.loads(
        (ROOT / "jenkins/config/components.json").read_text(encoding="utf-8")
    )
    assert {
        component["migrationPolicy"] for component in payload["components"]
    } <= {"none", "expand-only", "reversible"}


def test_modular_docker_builder_owns_exactly_the_seventeen_images():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "jenkins/scripts/build").glob("*.sh"))
    )
    images = set(re.findall(r'build_image "([^"]+)"', source))
    assert images == {
        "recsys-base-python",
        "recsys-data-ingestion",
        "recsys-feature-store",
        "recsys-drift-retrain",
        "recsys-spark",
        "recsys-flink",
        "recsys-airflow",
        "recsys-kafka-connect",
        "recsys-mlops-training",
        "recsys-mlops-spark",
        "recsys-mlflow",
        "recsys-api-serving",
        "recsys-demo-api",
        "recsys-demo-web",
        "recsys-analytics-spark",
        "recsys-analytics-dbt",
        "recsys-analytics-superset",
    }
    engine = (ROOT / "jenkins/scripts/build/engine.sh").read_text(encoding="utf-8")
    assert "flock -w" in engine
    assert "build_reuse_shared_image" in engine
    assert "BUILD_COMPONENT}-$$" in engine
    assert "already failed in this build" in engine
    assert "BUILD_SCAN_REPORT_DIR" in engine
    assert "container_scan_policy.py" in engine


def test_prometheus_operator_is_pinned_and_operator_only():
    source = (
        ROOT / "infra/terraform/gcp/dependencies.tf"
    ).read_text(encoding="utf-8")
    assert 'resource "helm_release" "prometheus_operator"' in source
    assert 'version          = "87.19.2"' in source
    assert 'name  = "prometheus.enabled"' in source
    assert 'name  = "prometheusOperator.enabled"' in source
    assert 'name  = "prometheusOperator.tls.enabled"' in source
