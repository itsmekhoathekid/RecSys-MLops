from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CONFIGURATION_PATH = ROOT / "jenkins/python/configuration.py"
SPEC = importlib.util.spec_from_file_location(
    "jenkins_configuration", CONFIGURATION_PATH
)
assert SPEC and SPEC.loader
configuration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configuration)

EXPECTED_STAGES = [
    "Checkout",
    "Detect Changed Components",
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
    "RAG Item Index",
    "RAG Retrieval API",
    "Online Feature API",
    "Inference API",
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
                "version": 2,
                "commit": "abc",
                "components": [
                    "materialize",
                    "training",
                    "dp1",
                    "dp2",
                    "dp3",
                    "analytics",
                ],
                "buildImages": [],
                "buildArtifacts": [],
                "deployUnits": [],
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


def test_stream_components_share_one_production_verification():
    completed = subprocess.run(
        [
            "bash",
            "-c",
            "source jenkins/scripts/test/dispatch.sh; "
            "component_verification_key stream_offline; "
            "component_verification_key stream_online",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.splitlines() == ["stream_features", "stream_features"]


def test_stream_online_runs_only_the_cross_boundary_contract():
    data_ci = (ROOT / "jenkins/scripts/ci/data.sh").read_text(encoding="utf-8")
    stream_online = data_ci.split("ci_stream_online()", 1)[1].split("\n}", 1)[0]

    assert "test_stream_online_serving_contract.py" in stream_online
    assert "tests/unit/api_serving/test_serving.py" not in stream_online


def test_rollout_deploy_uses_release_plan_namespace():
    entrypoint = (
        ROOT / "jenkins/scripts/entrypoints/release_deploy_unit.sh"
    ).read_text(encoding="utf-8")
    rollout = (ROOT / "jenkins/scripts/deploy/rollout.sh").read_text(encoding="utf-8")

    assert 'deploy_rollout_watcher "${unit_namespace}"' in entrypoint
    assert 'local namespace="$1"' in rollout
    assert "namespace_ci" not in rollout


def test_online_feature_deploy_takes_ownership_from_legacy_release():
    entrypoint = (
        ROOT / "jenkins/scripts/entrypoints/release_deploy_unit.sh"
    ).read_text(encoding="utf-8")

    assert '[[ "${unit_name}" == "online-feature-api" ]]' in entrypoint
    assert "helm_args+=(--take-ownership)" in entrypoint
    assert 'helm history "${unit_release}"' in entrypoint
    assert 'item.get("status") == "deployed"' in entrypoint
    assert '[[ "${deployed_revision_count}" == "0" ]]' in entrypoint
    assert "helm_failure_args=()" in entrypoint
    assert "using non-destructive initial ownership transfer" in entrypoint


def test_split_api_gcp_values_preserve_the_legacy_ml_node_placement():
    for chart in ("recsys-online-feature-api", "recsys-inference-api"):
        values = (ROOT / "infra/helm" / chart / "values-gcp.yaml").read_text(
            encoding="utf-8"
        )
        assert "recsys.ai/workload: ml-system" in values
        assert "effect: NoSchedule" in values


def test_online_feature_deploy_uses_canonical_registry_secret_without_cli_leakage():
    entrypoint = (
        ROOT / "jenkins/scripts/entrypoints/release_deploy_unit.sh"
    ).read_text(encoding="utf-8")

    assert '"recsys-data-platform-secret", "-o", "json"' in entrypoint
    assert 'chmod 600 "${sensitive_values_file}"' in entrypoint
    assert 'helm_args+=(-f "${sensitive_values_file}")' in entrypoint
    assert '--set-string "config.feastPostgresPassword=' not in entrypoint


def test_rag_promotion_contract_gate_uses_the_required_python_runtime():
    deployment = (ROOT / "jenkins/scripts/deploy/rag.sh").read_text(encoding="utf-8")
    contract_gate = deployment.split("rag_verify_api_contract()", 1)[1].split(
        "\n}", 1
    )[0]

    assert 'python3 - "${report}"' in contract_gate
    assert "jq -e" not in contract_gate
    assert "supported_embedding_contracts" in contract_gate


def test_rag_promotion_tunnels_to_a_running_pod_and_retries_readiness():
    deployment = (ROOT / "jenkins/scripts/deploy/rag.sh").read_text(encoding="utf-8")
    tunnel = deployment.split("rag_start_api_port_forward()", 1)[1].split(
        "\n}", 1
    )[0]
    gcp_values = (
        ROOT / "infra/helm/recsys-rag-api/values-gcp.yaml"
    ).read_text(encoding="utf-8")

    assert "--field-selector=status.phase=Running" in tunnel
    assert 'port-forward "pod/${ready_pod}"' in tunnel
    assert 'rollout status deployment/recsys-rag-api' in tunnel
    assert "for _ in $(seq 1 30)" in tunnel
    assert "recsys.ai/pool: cpu-services" in gcp_values


def test_rag_promotion_uses_pinned_embedding_batch_and_schedulable_request():
    deployment = (ROOT / "jenkins/scripts/deploy/rag.sh").read_text(encoding="utf-8")
    promotion = deployment.split("rag_index_promote()", 1)[1]

    assert promotion.count("embed-chunks") == 2
    assert promotion.count("--checkpoint-every 32") == 2
    assert 'requests: {cpu: 500m, memory: 2Gi}' in promotion
    assert "nodeSelector: {recsys.ai/workload: ml-system}" in promotion
    assert "value: ml-system, effect: NoSchedule" in promotion


def test_registry_push_refreshes_login_and_retries_once(tmp_path):
    attempt_path = tmp_path / "push-attempts"
    login_path = tmp_path / "registry-logins"
    push_log = tmp_path / "push.log"
    completed = subprocess.run(
        [
            "bash",
            "-c",
            r"""
set -euo pipefail
source jenkins/scripts/lib/common.sh
source jenkins/scripts/build/engine.sh
attempt_path="$1"
login_path="$2"
push_log="$3"
printf '0\n' >"${attempt_path}"
docker() {
  local count
  count="$(<"${attempt_path}")"
  count=$((count + 1))
  printf '%s\n' "${count}" >"${attempt_path}"
  if [[ "${count}" == "1" ]]; then
    printf 'unauthorized: authentication failed\n' >&2
    return 1
  fi
  printf 'digest: sha256:%064d\n' 0
}
registry_login_gcp() {
  printf 'login\n' >>"${login_path}"
}
BUILD_REGISTRY_HOST=asia-southeast1-docker.pkg.dev
BUILD_IMAGE_REGISTRY=asia-southeast1-docker.pkg.dev/example/recsys
BUILD_REGISTRY_LOGIN_EPOCH=0
push_built_image example/image:tag "${push_log}"
printf 'attempts=%s logins=%s\n' \
  "$(<"${attempt_path}")" \
  "$(wc -l <"${login_path}" | tr -d ' ')"
""",
            "registry-push-test",
            str(attempt_path),
            str(login_path),
            str(push_log),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "attempts=2 logins=1" in completed.stdout
    assert "digest: sha256:" in push_log.read_text(encoding="utf-8")


def test_deploy_preflight_refreshes_registry_login_before_digest_checks(tmp_path):
    login_path = tmp_path / "registry-login"
    plan_path = tmp_path / "release-plan.json"
    plan_path.write_text("{}\n", encoding="utf-8")
    completed = subprocess.run(
        [
            "bash",
            "-c",
            r"""
set -euo pipefail
source jenkins/scripts/lib/common.sh
source jenkins/scripts/deploy/preflight/gcp.sh
login_path="$1"
plan_path="$2"
gcp_production_field() {
  [[ "$1" == "imageRegistry" ]] || return 1
  printf '%s\n' 'asia-southeast1-docker.pkg.dev/example/recsys'
}
registry_login_gcp() {
  printf '%s\n' "$1" >"${login_path}"
}
image_manifest_lookup() {
  printf 'asia-southeast1-docker.pkg.dev/example/recsys/%s@sha256:%064d\n' "$1" 0
}
python3() {
  printf '%s\n' 'recsys-base-python'
}
docker() {
  [[ "$1 $2" == "manifest inspect" ]]
  [[ -s "${login_path}" ]]
}
gcp_verify_candidate_digests "${plan_path}"
""",
            "deploy-preflight-login-test",
            str(login_path),
            str(plan_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert login_path.read_text(encoding="utf-8").strip().endswith(
        "asia-southeast1-docker.pkg.dev/example/recsys"
    )


def test_full_jenkins_trigger_reuses_crumb_session_cookie():
    trigger = (ROOT / "ops/gcp/trigger_full_jenkins.sh").read_text(encoding="utf-8")

    assert '--cookie-jar "${cookie_file}"' in trigger
    assert '--cookie "${cookie_file}"' in trigger
    assert 'rm -f "${headers_file}" "${cookie_file}"' in trigger
    assert "online_feature_api,inference_api" in trigger
    assert ",api," not in trigger


def test_api_verification_uses_metric_available_before_live_traffic():
    serving = (ROOT / "jenkins/scripts/test/serving.sh").read_text(encoding="utf-8")

    assert "recsys_api_rollout_config_info" in serving
    assert '"model_predictions_total" in response.read()' not in serving
    assert "API image mismatch" in serving


def test_production_verification_is_fail_fast_and_kfp_check_is_read_only():
    runtime = (ROOT / "jenkins/scripts/test/runtime.sh").read_text(encoding="utf-8")
    ml_platform = (ROOT / "jenkins/scripts/test/ml_platform.sh").read_text(
        encoding="utf-8"
    )

    assert "set -euo pipefail\n    run_component_verification" in runtime
    assert "component_ci_python training" in ml_platform
    assert (
        '"${training_python}" apps/ml-system/src/kubeflow/verify_pipeline_upload.py'
        in (ml_platform)
    )
    assert "submit_pipeline_run.py" not in ml_platform
    assert "create_run_from_pipeline_package" not in ml_platform


def test_production_verification_does_not_launch_component_workloads():
    test_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "jenkins/scripts/test").glob("*.sh")
    )
    for forbidden in (
        "airflow dags trigger",
        "airflow dags unpause",
        "kubectl apply",
        "kubectl create",
        "kubectl run",
        "--verification-event-id",
        "submit_pipeline_run.py",
        "create_run_from_pipeline_package",
        "mesh_request POST",
        "dbt test",
    ):
        assert forbidden not in test_sources
    assert "component_test_airflow_dag_registered" in test_sources


def test_kfp_runtime_secret_contract_is_checked_without_reading_values():
    preflight = (ROOT / "jenkins/scripts/deploy/preflight/gcp.sh").read_text(
        encoding="utf-8"
    )
    terraform = (ROOT / "infra/terraform/gcp/secret_management.tf").read_text(
        encoding="utf-8"
    )
    for key in (
        "HUDI_DATASET_TABLE",
        "HUDI_CLEAN_HOURS_RETAINED",
        "HUDI_ZK_URL",
        "HUDI_ZK_PORT",
        "HUDI_ZK_BASE_PATH",
        "HUDI_ZK_LOCK_KEY",
    ):
        assert key in preflight
        assert key in terraform
    assert 'json.load(sys.stdin).get("data", {})' in preflight


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


def test_component_catalog_rejects_local_only_declared_file(monkeypatch):
    relative_path = "tests/unit/jenkins/test_cicd_configuration.py"
    assert (ROOT / relative_path).is_file()
    monkeypatch.setattr(configuration, "_tracked_paths", lambda: frozenset())

    with pytest.raises(ValueError, match="path is not tracked by Git"):
        configuration._validate_rule_paths(
            {"files": [relative_path]}, "local-only rule"
        )


def test_root_jenkins_stage_view_is_compact_and_keeps_internal_checkpoints():
    source = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")
    assert (
        re.findall(r"^\s*stage\('([^']+)'\)", source, flags=re.MULTILINE)
        == EXPECTED_STAGES
    )
    assert "skipDefaultCheckout" not in source
    assert "checkout scm" not in source
    assert "disableConcurrentBuilds()" in source
    assert "triggers {\n    githubPush()\n  }" in source
    assert "script: 'git rev-parse HEAD'" in source
    assert "values_args=(" not in source
    assert "source jenkins/scripts/" not in source
    assert "sh '''#!/usr/bin/env bash" in source
    assert ". jenkins/scripts/deploy/preflight/gcp.sh" in source
    assert "jenkins/scripts/lib/gcp.sh" not in source
    assert 'rm -rf "${CI_TMP_ROOT}"' in source
    assert "recovery_status" not in source
    pipeline_helper = (ROOT / "jenkins/pipeline/component_pipeline.groovy").read_text(
        encoding="utf-8"
    )
    assert "release_plan.py create" not in pipeline_helper
    assert "applyForcedComponents" not in pipeline_helper
    assert "selected.collate(maxParallel)" in pipeline_helper
    assert "params.PUBLISH_IMAGES && env.RUN_COMPONENT_DEPLOY" in pipeline_helper
    assert source.count("python3 jenkins/python/configuration.py validate") == 1
    assert source.count("release_deploy_preflight.sh") == 1
    assert "REQUIRE_GCP_ARTIFACT_REGISTRY='${params.PUBLISH_IMAGES" in source
    for marker in (
        "[CI] Contract checks",
        "[BUILD] Build and publish catalog images",
        "[PACKAGE] Compile Kubeflow package",
        "[DEPLOY] Production preflight",
        "[DEPLOY] Deploy release",
        "[VERIFY] Verify release",
    ):
        assert marker in source
    build_entrypoint = (
        ROOT / "jenkins/scripts/entrypoints/release_build_publish.sh"
    ).read_text(encoding="utf-8")
    assert "[BUILD] Build image ${image_index}/${image_total}" in build_entrypoint


def test_github_webhook_trigger_uses_pipeline_job_property() -> None:
    seed = (
        ROOT / "infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml"
    ).read_text(encoding="utf-8")

    trigger_property = (
        "org.jenkinsci.plugins.workflow.job.properties."
        "PipelineTriggersJobProperty"
    )
    assert seed.count(f"<{trigger_property}>") >= 2
    assert seed.count(f"</{trigger_property}>") >= 2
    assert "def triggerProperty = githubTrigger" in seed
    assert "${triggerProperty}" in seed
    assert "def triggerBlock = githubTrigger" not in seed
    assert "${triggerBlock}" not in seed


def test_gcp_production_target_is_strict_and_self_consistent():
    target = configuration.load_gcp_production()
    assert target == {
        "projectId": "recsys-mlops",
        "region": "asia-southeast1",
        "zone": "asia-southeast1-b",
        "cluster": "recsys-mlops-gke",
        "context": "gke_recsys-mlops_asia-southeast1-b_recsys-mlops-gke",
        "imageRegistry": "asia-southeast1-docker.pkg.dev/recsys-mlops/recsys",
    }


def test_component_ci_profiles_use_repo_locks():
    profiles = configuration.load_ci_environments()
    components = {
        component["name"]: component for component in configuration.load_components()
    }
    assert set(profiles) == {
        "data",
        "ml",
        "online-feature-api",
        "inference-api",
        "rag-api",
        "demo",
        "analytics",
    }
    assert components["online_feature_api"]["ciProfile"] == "online-feature-api"
    assert components["inference_api"]["ciProfile"] == "inference-api"
    assert components["rag_api"]["ciProfile"] == "rag-api"
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
    assert completed.stdout == "airflow-version-recsys-github-cicd-102-stream-online"
    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", completed.stdout)


def test_catalog_contains_only_supported_migration_policies():
    payload = json.loads(
        (ROOT / "jenkins/config/components.json").read_text(encoding="utf-8")
    )
    assert {component["migrationPolicy"] for component in payload["components"]} <= {
        "none",
        "expand-only",
        "reversible",
    }


def test_catalog_driven_builder_owns_exactly_seventeen_images():
    catalog = json.loads((ROOT / "images/catalog.json").read_text(encoding="utf-8"))
    assert catalog["version"] == 1
    assert len(catalog["images"]) == 17
    assert {name for name in catalog["images"] if name.endswith("-spark")} == {
        "recsys-spark"
    }
    assert all(spec["context"] == "." for spec in catalog["images"].values())
    engine = (ROOT / "jenkins/scripts/build/engine.sh").read_text(encoding="utf-8")
    assert "image_catalog.py build-spec" in engine
    assert "image_catalog.py dependencies" not in engine
    assert "flock" not in engine
    assert "build_reuse_shared_image" not in engine
    assert "build_publish_image" in engine


def test_locked_ml_images_match_exported_dependency_versions():
    root_project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    root_lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    ml_project = (ROOT / "apps/ml-system/pyproject.toml").read_text(
        encoding="utf-8"
    )
    ml_lock = (ROOT / "apps/ml-system/uv.lock").read_text(encoding="utf-8")
    assert '"gitpython==3.1.58"' in root_project
    assert 'name = "gitpython"\nversion = "3.1.58"' in root_lock
    assert '"gitpython==3.1.58"' in ml_project
    assert 'name = "gitpython"\nversion = "3.1.58"' in ml_lock

    for relative_path in (
        "images/data/recsys-spark/Dockerfile",
        "images/ml/recsys-mlops-training/Dockerfile",
        "images/ml/recsys-mlflow/Dockerfile",
    ):
        dockerfile = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "--constraint /tmp/ml-constraints.txt" in dockerfile
        assert "mlflow==3.15.1" in dockerfile
        assert "mlflow==3.14.0" not in dockerfile
        assert "cryptography==48.0.1" not in dockerfile

    training = (ROOT / "images/ml/recsys-mlops-training/Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "aiohttp==3.14.3" in training
    assert "ray_thirdparty" in training
    assert '"${ray_thirdparty}"/aiohttp-*.dist-info' in training
    assert '"${ray_thirdparty}/aiohttp-3.14.3.dist-info"' in training

    airflow = (ROOT / "images/data/recsys-airflow/Dockerfile").read_text(
        encoding="utf-8"
    )
    assert '"aiohttp==3.14.3"' in airflow
    assert '"cryptography==50.0.0"' in airflow
    assert '"aiohttp==3.13.3"' not in airflow
    assert '"cryptography==49.0.0"' not in airflow


def test_kubeflow_release_package_is_compiled_once_then_uploaded() -> None:
    package_entrypoint = (
        ROOT / "jenkins/scripts/entrypoints/release_package_artifacts.sh"
    ).read_text(encoding="utf-8")
    upload = (ROOT / "jenkins/scripts/deploy/upload_kfp_package.sh").read_text(
        encoding="utf-8"
    )
    deploy = (ROOT / "jenkins/scripts/entrypoints/release_deploy_unit.sh").read_text(
        encoding="utf-8"
    )

    assert package_entrypoint.count("jenkins/scripts/build/kfp_package.sh") == 1
    assert "kfp_package.sh" not in upload
    assert "compile_training_pipeline.py" not in upload
    assert "upload_kfp_package.sh" in deploy
    assert "kfp_version.sh" not in deploy


def test_seed_jobs_do_not_expose_retired_registry_parameters() -> None:
    seed = (
        ROOT / "infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml"
    ).read_text(encoding="utf-8")
    for parameter in (
        "IMAGE_PUSH_REGISTRY",
        "IMAGE_PULL_REGISTRY",
        "REQUIRE_GCP_ARTIFACT_REGISTRY",
        "DEPLOY_CHANGED_COMPONENTS",
    ):
        assert f"<name>{parameter}</name>" not in seed


def test_prometheus_operator_is_pinned_and_operator_only():
    source = (ROOT / "infra/terraform/gcp/dependencies.tf").read_text(encoding="utf-8")
    assert 'resource "helm_release" "prometheus_operator"' in source
    assert 'version          = "87.19.2"' in source
    assert 'name  = "prometheus.enabled"' in source
    assert 'name  = "prometheusOperator.enabled"' in source
    assert 'name  = "prometheusOperator.tls.enabled"' in source
