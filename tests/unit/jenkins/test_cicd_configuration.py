from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

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
    assert all(component["changeDetection"] for component in components)
    assert all("buildImages" in component for component in components)
    assert all("verifyDependsOn" in component for component in components)


def test_full_release_verification_orders_data_before_training(tmp_path):
    plan_path = tmp_path / "release-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "components": [
                    "materialize",
                    "training",
                    "dp1",
                    "dp2",
                    "dp3",
                    "analytics",
                ]
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "python3",
            "jenkins/python/release_plan.py",
            "plan-verifications",
            "--plan",
            str(plan_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    ordered = completed.stdout.splitlines()
    assert ordered.index("dp1") < ordered.index("dp2") < ordered.index("dp3")
    assert ordered.index("dp3") < ordered.index("materialize")
    assert ordered.index("materialize") < ordered.index("training")
    assert ordered.index("dp2") < ordered.index("analytics")


def test_component_catalog_rejects_misspelled_declared_path(tmp_path):
    payload = json.loads(
        (ROOT / "jenkins/config/components.json").read_text(encoding="utf-8")
    )
    payload["components"][0]["changeDetection"]["files"].append(
        "apps/data-platform/does-not-exist.py"
    )
    config_path = tmp_path / "components.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="path does not exist"):
        configuration.load_component_config(config_path)


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
    assert 'rm -rf "${CI_TMP_ROOT}"' in source
    assert "recovery_status" not in source
    pipeline_helper = (
        ROOT / "jenkins/pipeline/component_pipeline.groovy"
    ).read_text(encoding="utf-8")
    assert "release_plan.py create" in pipeline_helper
    assert "--output .ci-release-plan.json" in pipeline_helper
    assert "selected.collate(maxParallel)" in pipeline_helper
    assert "params.PUBLISH_IMAGES && env.RUN_COMPONENT_DEPLOY" in pipeline_helper
    assert "REQUIRE_GCP_ARTIFACT_REGISTRY='${params.PUBLISH_IMAGES" in source


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


def test_kubernetes_name_helper_emits_rfc1123_label():
    completed = subprocess.run(
        [
            "bash",
            "-c",
            (
                "source jenkins/scripts/lib/common.sh; "
                "recsys_kubernetes_name "
                "'Airflow.Version-RecSys_GitHub-CICD-102-stream_online'"
            ),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert (
        completed.stdout
        == "airflow-version-recsys-github-cicd-102-stream-online"
    )
    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", completed.stdout)


def test_catalog_contains_only_supported_migration_policies():
    payload = json.loads(
        (ROOT / "jenkins/config/components.json").read_text(encoding="utf-8")
    )
    assert {
        component["migrationPolicy"] for component in payload["components"]
    } <= {"none", "expand-only", "reversible"}


def test_catalog_driven_builder_owns_exactly_fifteen_images():
    catalog = json.loads((ROOT / "images/catalog.json").read_text(encoding="utf-8"))
    assert catalog["version"] == 1
    assert len(catalog["images"]) == 15
    assert {name for name in catalog["images"] if name.endswith("-spark")} == {
        "recsys-spark"
    }
    assert all(spec["context"] == "." for spec in catalog["images"].values())
    engine = (ROOT / "jenkins/scripts/build/engine.sh").read_text(encoding="utf-8")
    assert "image_catalog.py spec" in engine
    assert "image_catalog.py dependencies" in engine
    assert "image_catalog.py build-args" in engine
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
