from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


sys.path.append(str(Path("jenkins/scripts").resolve()))

import model_cd
from model_cd import REQUIRED_MODEL_FILES, main as model_cd_main


ROOT = Path(__file__).resolve().parents[2]


def _documents(rendered: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]


def test_api_serving_image_includes_feast_postgres_driver():
    dockerfile = (ROOT / "apps/api-serving/Dockerfile").read_text(encoding="utf-8")
    assert "feast[redis]" in dockerfile
    assert "psycopg[binary]" in dockerfile
    assert "psycopg-pool" in dockerfile


def test_serving_chart_renders_expected_namespaces():
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "recsys-serving",
            "infra/helm/recsys-serving",
            "--namespace",
            "kserve-triton-inference",
        ],
        text=True,
    )
    docs = _documents(rendered)
    by_kind_name = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}

    assert ("Namespace", "kserve-triton-inference") in by_kind_name
    assert ("Namespace", "api-serving") in by_kind_name
    inference_service = by_kind_name[("InferenceService", "recsys-bst-triton")]
    assert inference_service["metadata"]["namespace"] == "kserve-triton-inference"
    assert inference_service["metadata"]["annotations"]["serving.kserve.io/autoscalerClass"] == "external"
    predictor_model = inference_service["spec"]["predictor"]["model"]
    assert inference_service["spec"]["predictor"]["annotations"]["recsys.ai/triton-health-probes"] == (
        "v2-model-ready"
    )
    assert predictor_model["modelFormat"]["name"] == "triton"
    assert predictor_model["protocolVersion"] == "v2"
    assert predictor_model["storageUri"].startswith("s3://")
    triton_runtime = by_kind_name[("ClusterServingRuntime", "recsys-tritonserver")]
    triton_container = triton_runtime["spec"]["containers"][0]
    assert triton_container["startupProbe"]["httpGet"] == {"path": "/v2/health/ready", "port": "h2c"}
    assert triton_container["readinessProbe"]["httpGet"] == {"path": "/v2/health/ready", "port": "h2c"}
    assert triton_container["livenessProbe"]["httpGet"] == {"path": "/v2/health/live", "port": "h2c"}
    api_deployment = by_kind_name[("Deployment", "recsys-api-serving")]
    assert api_deployment["metadata"]["namespace"] == "api-serving"
    assert "replicas" not in api_deployment["spec"]
    assert api_deployment["spec"]["strategy"]["type"] == "RollingUpdate"
    assert api_deployment["spec"]["strategy"]["rollingUpdate"] == {
        "maxSurge": 1,
        "maxUnavailable": 0,
    }
    assert api_deployment["spec"]["minReadySeconds"] == 10
    assert api_deployment["spec"]["progressDeadlineSeconds"] == 120
    pod_metadata = api_deployment["spec"]["template"]["metadata"]
    assert "checksum/config" in pod_metadata["annotations"]
    api_container = api_deployment["spec"]["template"]["spec"]["containers"][0]
    assert api_container["startupProbe"]["httpGet"]["path"] == "/healthz"
    assert api_container["readinessProbe"]["httpGet"]["path"] == "/ready"
    assert api_container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    api_config = by_kind_name[("ConfigMap", "recsys-api-serving")]
    assert api_config["data"]["FORCE_NOT_READY"] == "0"
    assert api_config["data"]["ALLOW_FEATURE_FALLBACK"] == "0"
    assert api_config["data"]["RECSYS_JSON_LOGS"] == "1"
    assert api_config["data"]["OTEL_SERVICE_NAME"] == "recsys-api-serving"
    assert api_config["data"]["AB_SHADOW_ENABLED"] == "0"
    assert api_config["data"]["AB_SHADOW_SAMPLE_PERCENT"] == "100"
    assert ("ServiceMonitor", "recsys-api-serving") in by_kind_name
    assert ("HTTPScaledObject", "recsys-api-serving-http") not in by_kind_name
    api_scaledobject = by_kind_name[("ScaledObject", "recsys-api-serving-prometheus")]
    assert api_scaledobject["metadata"]["namespace"] == "api-serving"
    assert api_scaledobject["spec"]["scaleTargetRef"] == {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": "recsys-api-serving",
    }
    assert api_scaledobject["spec"]["minReplicaCount"] == 1
    assert api_scaledobject["spec"]["maxReplicaCount"] == 3
    assert api_scaledobject["spec"]["advanced"]["horizontalPodAutoscalerConfig"]["name"] == "recsys-api-serving"
    assert [trigger["type"] for trigger in api_scaledobject["spec"]["triggers"]] == ["prometheus", "prometheus"]
    api_request_query = api_scaledobject["spec"]["triggers"][0]["metadata"]["query"]
    assert "recsys_api_requests_total" in api_request_query
    assert f'service="{api_config["data"]["OTEL_SERVICE_NAME"]}"' in api_request_query
    assert "recsys_api_request_duration_seconds_sum" in api_scaledobject["spec"]["triggers"][1]["metadata"]["query"]
    feature_api_deployment = by_kind_name[("Deployment", "recsys-online-feature-api")]
    assert "replicas" not in feature_api_deployment["spec"]
    feature_api_scaledobject = by_kind_name[("ScaledObject", "recsys-online-feature-api-prometheus")]
    feature_api_config = by_kind_name[("ConfigMap", "recsys-online-feature-api")]
    assert feature_api_scaledobject["metadata"]["namespace"] == "api-serving"
    assert feature_api_scaledobject["spec"]["scaleTargetRef"]["name"] == "recsys-online-feature-api"
    assert feature_api_scaledobject["spec"]["maxReplicaCount"] == 3
    feature_api_request_query = feature_api_scaledobject["spec"]["triggers"][0]["metadata"]["query"]
    assert f'service="{feature_api_config["data"]["OTEL_SERVICE_NAME"]}"' in feature_api_request_query
    assert ("HTTPScaledObject", "recsys-bst-triton-http") not in by_kind_name
    assert ("Service", "recsys-bst-triton-http") not in by_kind_name
    kserve_resource_scaledobject = by_kind_name[("ScaledObject", "recsys-bst-triton-resource")]
    assert kserve_resource_scaledobject["metadata"]["namespace"] == "kserve-triton-inference"
    assert kserve_resource_scaledobject["metadata"]["annotations"][
        "scaledobject.keda.sh/transfer-hpa-ownership"
    ] == "true"
    assert (
        kserve_resource_scaledobject["spec"]["advanced"]["horizontalPodAutoscalerConfig"]["name"]
        == "recsys-bst-triton-predictor"
    )
    assert kserve_resource_scaledobject["spec"]["scaleTargetRef"]["name"] == "recsys-bst-triton-predictor"
    assert kserve_resource_scaledobject["spec"]["minReplicaCount"] == 1
    assert kserve_resource_scaledobject["spec"]["maxReplicaCount"] == 3
    assert kserve_resource_scaledobject["spec"]["triggers"] == [
        {
            "type": "cpu",
            "metricType": "Utilization",
            "metadata": {"value": "50"},
        }
    ]


def test_serving_chart_can_render_api_only_for_rollout_demo():
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "recsys-serving",
            "infra/helm/recsys-serving",
            "--namespace",
            "kserve-triton-inference",
            "--set",
            "kserve.enabled=false",
        ],
        text=True,
    )
    docs = _documents(rendered)
    by_kind_name = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}

    assert ("Deployment", "recsys-api-serving") in by_kind_name
    assert ("Service", "recsys-api-serving") in by_kind_name
    assert ("HTTPScaledObject", "recsys-api-serving-http") not in by_kind_name
    assert ("ScaledObject", "recsys-api-serving-prometheus") in by_kind_name
    assert ("ScaledObject", "recsys-online-feature-api-prometheus") in by_kind_name
    assert ("HTTPScaledObject", "recsys-bst-triton-http") not in by_kind_name
    assert ("ScaledObject", "recsys-bst-triton-resource") in by_kind_name
    assert ("InferenceService", "recsys-bst-triton") not in by_kind_name
    assert ("ClusterServingRuntime", "kserve-tritonserver") not in by_kind_name


def test_api_component_deploy_does_not_disable_kserve_autoscaling():
    deploy_script = (ROOT / "jenkins/scripts/component_deploy.sh").read_text(encoding="utf-8")
    api_deploy = re.search(r"deploy_api_unlocked\(\) \{(?P<body>.*?)\n\}", deploy_script, re.DOTALL)

    assert api_deploy is not None
    assert 'kserve.enabled=false' not in api_deploy.group("body")
    assert 'autoscaling.kserveResource.enabled=false' not in api_deploy.group("body")
    assert "--wait" not in api_deploy.group("body")
    assert 'verify_and_wait_workload "deployment" "recsys-api-serving"' in api_deploy.group("body")


def test_serving_chart_renders_candidate_for_ab_testing():
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "recsys-serving",
            "infra/helm/recsys-serving",
            "--namespace",
            "kserve-triton-inference",
            "--set",
            "abTest.enabled=true",
            "--set",
            "abTest.experimentId=exp-1",
            "--set",
            "abTest.candidateWeightPercent=10",
            "--set",
            "abTest.controlModelVersion=stable-001",
            "--set",
            "abTest.candidateModelVersion=candidate-001",
            "--set",
            "kserve.inferenceService.candidateStorageUri=s3://recsys-model-store/triton/bst/candidate-001",
        ],
        text=True,
    )
    docs = _documents(rendered)
    by_kind_name = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}

    candidate = by_kind_name[("InferenceService", "recsys-bst-triton-candidate")]
    assert candidate["spec"]["predictor"]["model"]["storageUri"] == (
        "s3://recsys-model-store/triton/bst/candidate-001"
    )
    assert candidate.get("metadata", {}).get("annotations", {}).get("helm.sh/resource-policy") is None
    candidate_grpc = by_kind_name[("Service", "recsys-bst-triton-candidate-grpc")]
    assert candidate_grpc["spec"]["selector"] == {"app": "isvc.recsys-bst-triton-candidate-predictor"}
    candidate_scaledobject = by_kind_name[("ScaledObject", "recsys-bst-triton-candidate-resource")]
    assert candidate_scaledobject["spec"]["scaleTargetRef"]["name"] == "recsys-bst-triton-candidate-predictor"
    api_config = by_kind_name[("ConfigMap", "recsys-api-serving")]
    assert api_config["data"]["AB_TEST_ENABLED"] == "1"
    assert api_config["data"]["AB_CANDIDATE_WEIGHT_PERCENT"] == "10"
    assert api_config["data"]["AB_CONTROL_MODEL_VERSION"] == "stable-001"
    assert api_config["data"]["AB_CANDIDATE_MODEL_VERSION"] == "candidate-001"


def test_serving_chart_renders_candidate_for_shadow_with_zero_ab_weight():
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "recsys-serving",
            "infra/helm/recsys-serving",
            "--namespace",
            "kserve-triton-inference",
            "--set",
            "shadow.enabled=true",
            "--set",
            "abTest.enabled=false",
            "--set",
            "abTest.candidateWeightPercent=0",
            "--set",
            "kserve.inferenceService.candidateStorageUri=s3://recsys-model-store/triton/bst/shadow-001",
        ],
        text=True,
    )
    docs = _documents(rendered)
    by_kind_name = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}

    assert ("InferenceService", "recsys-bst-triton-candidate") in by_kind_name
    assert ("Service", "recsys-bst-triton-candidate-grpc") in by_kind_name
    api_config = by_kind_name[("ConfigMap", "recsys-api-serving")]
    assert api_config["data"]["AB_SHADOW_ENABLED"] == "1"
    assert api_config["data"]["AB_TEST_ENABLED"] == "0"
    assert api_config["data"]["AB_CANDIDATE_WEIGHT_PERCENT"] == "0"


def test_model_cd_writes_shadow_and_explicit_rollback_values(tmp_path):
    control = {"model_name": "bst", "model_version": "stable-001", "triton_storage_uri": "/control"}
    candidate = {"model_name": "bst", "model_version": "candidate-001", "triton_storage_uri": "/candidate"}

    shadow_path = model_cd.write_values(
        control,
        tmp_path / "shadow",
        control_manifest=control,
        candidate_manifest=candidate,
        stage="shadow-start",
        candidate_weight_percent=0,
        experiment_id="exp-shadow",
    )
    shadow_values = json.loads(shadow_path.read_text(encoding="utf-8"))
    assert shadow_values["shadow"]["enabled"] is True
    assert shadow_values["kserve"]["enabled"] is True
    assert shadow_values["kserve"]["secret"]["create"] is False
    assert shadow_values["abTest"]["enabled"] is False
    assert shadow_values["abTest"]["candidateWeightPercent"] == 0
    assert shadow_values["abTest"]["candidateTritonUrl"].startswith(
        "recsys-bst-triton-candidate-predictor."
    )
    assert shadow_values["abTest"]["controlTritonUrl"].startswith(
        "recsys-bst-triton-predictor."
    )
    assert shadow_values["kserve"]["inferenceService"]["candidateStorageUri"] == "/candidate"
    assert shadow_values["api"]["rollout"]["maxUnavailable"] == 0
    assert shadow_values["api"]["rollout"]["maxSurge"] == 1
    assert shadow_values["autoscaling"]["prometheus"]["api"]["minReplicas"] == 2

    rollback_path = model_cd.write_values(
        control,
        tmp_path / "rollback",
        stage="rollback",
        candidate_weight_percent=50,
        experiment_id="exp-shadow",
    )
    rollback_values = json.loads(rollback_path.read_text(encoding="utf-8"))
    assert rollback_values["shadow"]["enabled"] is False
    assert rollback_values["kserve"]["enabled"] is True
    assert rollback_values["kserve"]["secret"]["create"] is False
    assert rollback_values["abTest"]["enabled"] is False
    assert rollback_values["abTest"]["candidateWeightPercent"] == 0
    assert rollback_values["abTest"]["candidateTritonUrl"] == ""
    assert rollback_values["kserve"]["inferenceService"]["candidateStorageUri"] == ""

    if shutil.which("helm") is not None:
        shadow_rendered = subprocess.check_output(
            ["helm", "template", "recsys-serving", "infra/helm/recsys-serving", "-f", str(shadow_path)],
            text=True,
        )
        rollback_rendered = subprocess.check_output(
            ["helm", "template", "recsys-serving", "infra/helm/recsys-serving", "-f", str(rollback_path)],
            text=True,
        )
        shadow_resources = {(doc["kind"], doc["metadata"]["name"]) for doc in _documents(shadow_rendered)}
        rollback_resources = {(doc["kind"], doc["metadata"]["name"]) for doc in _documents(rollback_rendered)}
        assert ("InferenceService", "recsys-bst-triton-candidate") in shadow_resources
        assert ("InferenceService", "recsys-bst-triton-candidate") not in rollback_resources


def test_model_cd_can_retain_candidate_while_switching_api_to_promoted_stable(tmp_path):
    promoted = {"model_name": "bst", "model_version": "candidate-001", "triton_storage_uri": "/candidate"}
    values_path = model_cd.write_values(
        promoted,
        tmp_path / "retained",
        control_manifest=promoted,
        candidate_manifest=promoted,
        stage="deploy",
        retain_candidate=True,
    )
    values = json.loads(values_path.read_text(encoding="utf-8"))

    assert values["abTest"]["enabled"] is False
    assert values["kserve"]["inferenceService"]["retainCandidate"] is True
    rendered = subprocess.check_output(
        ["helm", "template", "recsys-serving", "infra/helm/recsys-serving", "-f", str(values_path)],
        text=True,
    )
    resources = {(doc["kind"], doc["metadata"]["name"]) for doc in _documents(rendered)}
    assert ("InferenceService", "recsys-bst-triton-candidate") in resources


def test_model_cd_evaluate_writes_decision_and_auto_renders_rollback(tmp_path, monkeypatch):
    control_repo = tmp_path / "control-repo"
    candidate_repo = tmp_path / "candidate-repo"
    for root in [control_repo, candidate_repo]:
        for relative in REQUIRED_MODEL_FILES:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test")
    control_manifest = tmp_path / "control.json"
    candidate_manifest = tmp_path / "candidate.json"
    control_manifest.write_text(
        json.dumps(
            {"model_name": "bst", "model_version": "stable-001", "triton_storage_uri": str(control_repo)}
        ),
        encoding="utf-8",
    )
    candidate_manifest.write_text(
        json.dumps(
            {"model_name": "bst", "model_version": "candidate-001", "triton_storage_uri": str(candidate_repo)}
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        model_cd,
        "evaluate_candidate_gates",
        lambda *_args, **_kwargs: model_cd.GateDecision(
            "rollback",
            ["candidate error gate failed"],
            {"candidate_error_rate": 0.2, "control_error_rate": 0.01},
            "exp-auto-rollback",
            "10m",
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "model_cd.py",
            "--stage",
            "evaluate",
            "--control-manifest-uri",
            str(control_manifest),
            "--candidate-manifest-uri",
            str(candidate_manifest),
            "--experiment-id",
            "exp-auto-rollback",
            "--prometheus-url",
            "http://prometheus",
            "--output-dir",
            str(output_dir),
        ],
    )

    assert model_cd_main() == 0
    decision = json.loads((output_dir / "ab-decision.json").read_text(encoding="utf-8"))
    values = json.loads((output_dir / "recsys-serving-values.json").read_text(encoding="utf-8"))
    deployed = json.loads((output_dir / "deployed-model.json").read_text(encoding="utf-8"))
    assert decision["decision"] == "rollback"
    assert values["abTest"]["candidateWeightPercent"] == 0
    assert values["shadow"]["enabled"] is False
    assert values["kserve"]["inferenceService"]["candidateStorageUri"] == ""
    assert deployed["stage"] == "rollback"


def test_model_cd_validates_local_manifest_and_writes_values(tmp_path, monkeypatch):
    model_repo = tmp_path / "model-repo"
    for relative in REQUIRED_MODEL_FILES:
        path = model_repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "model_name": "bst",
                "model_version": "trial-001",
                "triton_storage_uri": str(model_repo),
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "model_cd.py",
            "--manifest-uri",
            str(manifest),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert model_cd_main() == 0
    values = json.loads((output_dir / "recsys-serving-values.json").read_text(encoding="utf-8"))
    assert values["kserve"]["namespace"]["name"] == "kserve-triton-inference"
    assert values["api"]["namespace"]["name"] == "api-serving"
    assert values["api"]["config"]["modelVersion"] == "trial-001"
    assert values["abTest"]["enabled"] is False


def test_model_cd_writes_ab_start_values(tmp_path, monkeypatch):
    control_repo = tmp_path / "control-repo"
    candidate_repo = tmp_path / "candidate-repo"
    for root in [control_repo, candidate_repo]:
        for relative in REQUIRED_MODEL_FILES:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test")
    control_manifest = tmp_path / "control.json"
    candidate_manifest = tmp_path / "candidate.json"
    control_manifest.write_text(
        json.dumps(
            {
                "model_name": "bst",
                "model_version": "stable-001",
                "triton_storage_uri": str(control_repo),
                "serving_storage_uri": str(control_repo),
            }
        ),
        encoding="utf-8",
    )
    candidate_manifest.write_text(
        json.dumps(
            {
                "model_name": "bst",
                "model_version": "candidate-001",
                "triton_storage_uri": str(candidate_repo),
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "model_cd.py",
            "--stage",
            "ab-start",
            "--control-manifest-uri",
            str(control_manifest),
            "--candidate-manifest-uri",
            str(candidate_manifest),
            "--candidate-weight-percent",
            "10",
            "--experiment-id",
            "exp-1",
            "--output-dir",
            str(output_dir),
        ],
    )

    assert model_cd_main() == 0
    values = json.loads((output_dir / "recsys-serving-values.json").read_text(encoding="utf-8"))

    assert values["kserve"]["inferenceService"]["storageUri"] == str(control_repo)
    assert values["kserve"]["inferenceService"]["candidateStorageUri"] == str(candidate_repo)
    assert values["abTest"]["enabled"] is True
    assert values["abTest"]["candidateWeightPercent"] == 10
    assert values["abTest"]["experimentId"] == "exp-1"
    assert values["abTest"]["controlModelVersion"] == "stable-001"
    assert values["abTest"]["candidateModelVersion"] == "candidate-001"


def test_model_cd_rollback_disables_ab_values(tmp_path, monkeypatch):
    model_repo = tmp_path / "model-repo"
    for relative in REQUIRED_MODEL_FILES:
        path = model_repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "model_name": "bst",
                "model_version": "stable-001",
                "triton_storage_uri": str(model_repo),
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "model_cd.py",
            "--stage",
            "rollback",
            "--manifest-uri",
            str(manifest),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert model_cd_main() == 0
    values = json.loads((output_dir / "recsys-serving-values.json").read_text(encoding="utf-8"))

    assert values["abTest"]["enabled"] is False
    assert values["abTest"]["candidateWeightPercent"] == 0


def test_model_cd_promote_dry_run_renders_candidate_as_stable(tmp_path, monkeypatch):
    control_repo = tmp_path / "control-repo"
    candidate_repo = tmp_path / "candidate-repo"
    for root in [control_repo, candidate_repo]:
        for relative in REQUIRED_MODEL_FILES:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test")
    control_manifest = tmp_path / "control.json"
    candidate_manifest = tmp_path / "candidate.json"
    latest_repo = tmp_path / "latest"
    control_manifest.write_text(
        json.dumps(
            {
                "model_name": "bst",
                "model_version": "stable-001",
                "triton_storage_uri": str(control_repo),
                "serving_storage_uri": str(latest_repo),
            }
        ),
        encoding="utf-8",
    )
    candidate_manifest.write_text(
        json.dumps(
            {
                "model_name": "bst",
                "model_version": "candidate-001",
                "triton_storage_uri": str(candidate_repo),
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "model_cd.py",
            "--stage",
            "promote",
            "--control-manifest-uri",
            str(control_manifest),
            "--candidate-manifest-uri",
            str(candidate_manifest),
            "--manifest-uri",
            str(tmp_path / "latest.json"),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert model_cd_main() == 0
    values = json.loads((output_dir / "recsys-serving-values.json").read_text(encoding="utf-8"))

    assert values["kserve"]["inferenceService"]["storageUri"] == str(latest_repo)
    assert values["api"]["config"]["modelVersion"] == "candidate-001"
    assert values["abTest"]["enabled"] is False


def test_model_cd_deploy_uses_atomic_helm_upgrade(monkeypatch, tmp_path):
    commands = []

    def fake_run(command: list[str]) -> None:
        commands.append(command)

    monkeypatch.setattr(model_cd, "run", fake_run)
    monkeypatch.setattr(model_cd, "crd_exists", lambda _: True)
    values_path = tmp_path / "values.json"
    values_path.write_text("{}", encoding="utf-8")

    model_cd.deploy(values_path, timeout="90s")

    helm_upgrade = commands[1]
    final_helm_upgrade = commands[-1]
    assert helm_upgrade[:4] == ["helm", "upgrade", "--install", "recsys-serving"]
    assert "--atomic" in helm_upgrade
    assert helm_upgrade[helm_upgrade.index("--timeout") + 1] == "90s"
    assert "autoscaling.kserveResource.enabled=false" in helm_upgrade
    assert "autoscaling.kserveResource.enabled=true" in final_helm_upgrade


def test_model_cd_deploy_can_disable_atomic_and_servicemonitor(monkeypatch, tmp_path):
    commands = []

    def fake_run(command: list[str]) -> None:
        commands.append(command)

    monkeypatch.setenv("RECSYS_MODEL_CD_ATOMIC", "0")
    monkeypatch.setattr(model_cd, "run", fake_run)
    monkeypatch.setattr(model_cd, "crd_exists", lambda _: False)
    values_path = tmp_path / "values.json"
    values_path.write_text("{}", encoding="utf-8")

    model_cd.deploy(values_path, timeout="90s")

    helm_upgrade = commands[1]
    final_helm_upgrade = commands[-1]
    assert "--atomic" not in helm_upgrade
    assert "observability.serviceMonitor.enabled=false" in helm_upgrade
    assert "autoscaling.kserveResource.enabled=true" in final_helm_upgrade
    assert "observability.serviceMonitor.enabled=false" in final_helm_upgrade


def test_model_cd_deploy_waits_for_shadow_candidate(monkeypatch, tmp_path):
    commands = []

    monkeypatch.setattr(model_cd, "run", lambda command: commands.append(command))
    monkeypatch.setattr(model_cd, "crd_exists", lambda _: True)
    values_path = tmp_path / "values.json"
    values_path.write_text(
        json.dumps(
            {
                "kserve": {"inferenceService": {"candidateStorageUri": "s3://store/candidate"}},
                "abTest": {"enabled": False},
                "shadow": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )

    model_cd.deploy(values_path, timeout="90s")

    flattened = [" ".join(command) for command in commands]
    assert any("inferenceservice/recsys-bst-triton-candidate" in command for command in flattened)
    assert any("deployment/recsys-bst-triton-candidate-predictor" in command for command in flattened)


def test_model_cd_deploy_waits_for_retained_candidate(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(model_cd, "run", lambda command: commands.append(command))
    monkeypatch.setattr(model_cd, "crd_exists", lambda _: True)
    values_path = tmp_path / "values.json"
    values_path.write_text(
        json.dumps(
            {
                "kserve": {
                    "inferenceService": {
                        "candidateStorageUri": "s3://store/candidate",
                        "retainCandidate": True,
                    }
                },
                "abTest": {"enabled": False},
                "shadow": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )

    model_cd.deploy(values_path, timeout="90s")

    flattened = [" ".join(command) for command in commands]
    assert any("inferenceservice/recsys-bst-triton-candidate" in command for command in flattened)


def test_model_cd_s3_helpers_copy_upload_and_read(monkeypatch):
    class Body:
        def read(self):
            return b'{"model_name": "bst"}'

    class Paginator:
        def paginate(self, Bucket, Prefix):
            assert Bucket == "source"
            assert Prefix == "models/"
            return [{"Contents": [{"Key": "models/a.pb"}, {"Key": "models/nested/b.pb"}]}]

    class Client:
        def __init__(self):
            self.copied = []
            self.uploads = []
            self.heads = []

        def get_object(self, Bucket, Key):
            assert (Bucket, Key) == ("bucket", "manifest.json")
            return {"Body": Body()}

        def head_object(self, Bucket, Key):
            self.heads.append((Bucket, Key))

        def get_paginator(self, name):
            assert name == "list_objects_v2"
            return Paginator()

        def copy_object(self, Bucket, Key, CopySource):
            self.copied.append((Bucket, Key, CopySource))

        def put_object(self, **kwargs):
            self.uploads.append(kwargs)

    client = Client()
    monkeypatch.setattr(model_cd, "s3_client", lambda: client)

    assert model_cd.parse_s3_uri("s3://bucket/manifest.json") == ("bucket", "manifest.json")
    with pytest.raises(ValueError):
        model_cd.parse_s3_uri("file:///tmp/model")
    assert model_cd.read_manifest("s3://bucket/manifest.json") == {"model_name": "bst"}

    model_cd.verify_model_repository("s3://bucket/model")
    assert len(client.heads) == len(REQUIRED_MODEL_FILES)

    model_cd.copy_s3_prefix("s3://source/models", "s3://target/prod")
    assert client.copied == [
        ("target", "prod/a.pb", {"Bucket": "source", "Key": "models/a.pb"}),
        ("target", "prod/nested/b.pb", {"Bucket": "source", "Key": "models/nested/b.pb"}),
    ]

    model_cd.upload_manifest({"version": "v1"}, "s3://target/latest.json")
    assert client.uploads[0]["Bucket"] == "target"
    assert client.uploads[0]["Key"] == "latest.json"


def test_model_cd_s3_client_prefers_model_store_endpoint(monkeypatch):
    calls = []

    class Boto3:
        def client(self, *args, **kwargs):
            calls.append((args, kwargs))
            return object()

    monkeypatch.setitem(sys.modules, "boto3", Boto3())
    monkeypatch.setenv("MODEL_STORE_ENDPOINT", "http://model-store:9000")
    monkeypatch.setenv("MLFLOW_S3_ENDPOINT_URL", "http://mlflow-minio:9000")
    monkeypatch.setenv("MINIO_ENDPOINT", "http://data-minio:9000")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

    model_cd.s3_client()

    assert calls == [
        (
            ("s3",),
            {
                "endpoint_url": "http://model-store:9000",
                "aws_access_key_id": "access",
                "aws_secret_access_key": "secret",
                "region_name": "us-east-1",
            },
        )
    ]


def test_model_cd_missing_local_repository_and_latest_uri(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError):
        model_cd.verify_model_repository(str(tmp_path / "missing"))

    assert model_cd.latest_storage_uri({"serving_storage_uri": "s3://store/prod"}, {"model_version": "v1"}) == "s3://store/prod"
    monkeypatch.setenv("MODEL_STORE_BUCKET", "bucket")
    monkeypatch.setenv("MODEL_STORE_PREFIX", "models/bst")
    assert model_cd.latest_storage_uri(None, {"model_version": "v1"}) == "s3://bucket/models/bst/latest"


def test_model_cd_prometheus_gates(monkeypatch):
    values = {
        "candidate_error": 0.01,
        "control_error": 0.02,
        "candidate_latency": 0.10,
        "control_latency": 0.10,
    }

    def fake_query(_url, query):
        if 'status="error"' in query and 'ab_variant="candidate"' in query:
            return values["candidate_error"]
        if 'status="error"' in query and 'ab_variant="control"' in query:
            return values["control_error"]
        if 'ab_variant="candidate"' in query:
            return values["candidate_latency"]
        return values["control_latency"]

    monkeypatch.setattr(model_cd, "query_prometheus", fake_query)
    model_cd.assert_promote_gates("http://prometheus", "10m")

    values["candidate_error"] = 0.20
    with pytest.raises(RuntimeError, match="candidate error gate failed"):
        model_cd.assert_promote_gates("http://prometheus", "10m")

    values["candidate_error"] = 0.01
    values["candidate_latency"] = 1.0
    with pytest.raises(RuntimeError, match="candidate latency gate failed"):
        model_cd.assert_promote_gates("http://prometheus", "10m")


def test_model_cd_gate_decision_filters_experiment_and_covers_hold_promote_rollback(monkeypatch):
    values = {
        "candidate_samples": 200.0,
        "control_samples": 250.0,
        "candidate_error": 0.01,
        "control_error": 0.01,
        "candidate_latency": 0.10,
        "control_latency": 0.10,
        "candidate_quality": 0.80,
        "control_quality": 0.82,
    }
    queries = []

    def fake_query(_url, query):
        queries.append(query)
        candidate = 'ab_variant="candidate"' in query
        if "increase(model_predictions_total" in query:
            return values["candidate_samples" if candidate else "control_samples"]
        if 'status="error"' in query:
            return values["candidate_error" if candidate else "control_error"]
        if "model_prediction_latency_seconds_bucket" in query:
            return values["candidate_latency" if candidate else "control_latency"]
        return values["candidate_quality" if candidate else "control_quality"]

    monkeypatch.setattr(model_cd, "query_prometheus", fake_query)
    decision = model_cd.evaluate_candidate_gates(
        "http://prometheus",
        "10m",
        experiment_id="exp-gated",
        min_samples=100,
    )
    assert decision.decision == "promote"
    assert all('experiment_id="exp-gated"' in query for query in queries)

    values["candidate_samples"] = 5
    assert model_cd.evaluate_candidate_gates("http://prometheus", "10m", min_samples=100).decision == "hold"

    values["candidate_samples"] = 200
    values["candidate_error"] = 0.20
    rollback = model_cd.evaluate_candidate_gates("http://prometheus", "10m", min_samples=100)
    assert rollback.decision == "rollback"
    assert "candidate error gate failed" in rollback.reasons[0]

    values["candidate_error"] = 0.01
    values["candidate_quality"] = 0.20
    rollback = model_cd.evaluate_candidate_gates("http://prometheus", "10m", min_samples=100)
    assert rollback.decision == "rollback"
    assert any("quality proxy" in reason for reason in rollback.reasons)


def test_model_cd_query_prometheus_and_crd_exists(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data": {"result": [{"value": [1, "2.5"]}]}}'

    requested = {}

    def fake_urlopen(url, timeout):
        requested["url"] = url
        requested["timeout"] = timeout
        return Response()

    monkeypatch.setattr(model_cd.urllib.request, "urlopen", fake_urlopen)
    assert model_cd.query_prometheus("http://prometheus", "sum(rate(x[5m]))") == 2.5
    assert "sum%28rate%28x%5B5m%5D%29%29" in requested["url"]
    assert requested["timeout"] == 15

    monkeypatch.setattr(model_cd.subprocess, "run", lambda *args, **kwargs: type("Result", (), {"returncode": 0})())
    assert model_cd.crd_exists("servicemonitors.monitoring.coreos.com") is True
    monkeypatch.setattr(model_cd.subprocess, "run", lambda *args, **kwargs: type("Result", (), {"returncode": 1})())
    assert model_cd.crd_exists("missing.example.com") is False


def test_kserve_component_cicd_validates_only_and_cd_job_applies_model_deploy():
    deploy_script = (ROOT / "jenkins/scripts/component_deploy.sh").read_text(encoding="utf-8")
    cicd_block = re.search(
        r"deploy_kserve_unlocked\(\) \{(?P<body>.*?)\n\}",
        deploy_script,
        flags=re.S,
    )
    cd_block = re.search(
        r"deploy_kserve_model_cd_unlocked\(\) \{(?P<body>.*?)\n\}",
        deploy_script,
        flags=re.S,
    )

    assert cicd_block is not None
    assert cd_block is not None
    assert "model_cd.py" in cicd_block.group("body")
    assert "--apply" not in cicd_block.group("body")
    assert "model_cd.py" in cd_block.group("body")
    assert "--apply" in cd_block.group("body")
    assert "kserve_model_cd)" in deploy_script
