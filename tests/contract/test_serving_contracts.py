from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


sys.path.append(str(Path("jenkins/scripts").resolve()))

import model_cd
from model_cd import REQUIRED_MODEL_FILES, main as model_cd_main


def _documents(rendered: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]


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
    assert inference_service["spec"]["predictor"]["triton"]["storageUri"].startswith("s3://")
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
    assert ("ServiceMonitor", "recsys-api-serving") in by_kind_name
    api_http_scaledobject = by_kind_name[("HTTPScaledObject", "recsys-api-serving-http")]
    assert api_http_scaledobject["metadata"]["namespace"] == "api-serving"
    assert api_http_scaledobject["spec"]["hosts"] == ["recsys-api-serving.local"]
    assert api_http_scaledobject["spec"]["scaleTargetRef"] == {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": "recsys-api-serving",
        "service": "recsys-api-serving",
        "port": 80,
    }
    assert api_http_scaledobject["spec"]["scalingMetric"]["requestRate"]["targetValue"] == 5
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
    assert ("HTTPScaledObject", "recsys-api-serving-http") in by_kind_name
    assert ("HTTPScaledObject", "recsys-bst-triton-http") not in by_kind_name
    assert ("ScaledObject", "recsys-bst-triton-resource") in by_kind_name
    assert ("InferenceService", "recsys-bst-triton") not in by_kind_name
    assert ("ClusterServingRuntime", "kserve-tritonserver") not in by_kind_name


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
    assert candidate["spec"]["predictor"]["triton"]["storageUri"] == (
        "s3://recsys-model-store/triton/bst/candidate-001"
    )
    candidate_grpc = by_kind_name[("Service", "recsys-bst-triton-candidate-grpc")]
    assert candidate_grpc["spec"]["selector"] == {"app": "isvc.recsys-bst-triton-candidate-predictor"}
    candidate_scaledobject = by_kind_name[("ScaledObject", "recsys-bst-triton-candidate-resource")]
    assert candidate_scaledobject["spec"]["scaleTargetRef"]["name"] == "recsys-bst-triton-candidate-predictor"
    api_config = by_kind_name[("ConfigMap", "recsys-api-serving")]
    assert api_config["data"]["AB_TEST_ENABLED"] == "1"
    assert api_config["data"]["AB_CANDIDATE_WEIGHT_PERCENT"] == "10"
    assert api_config["data"]["AB_CONTROL_MODEL_VERSION"] == "stable-001"
    assert api_config["data"]["AB_CANDIDATE_MODEL_VERSION"] == "candidate-001"


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
    values_path = tmp_path / "values.json"
    values_path.write_text("{}", encoding="utf-8")

    model_cd.deploy(values_path, timeout="90s")

    helm_upgrade = commands[1]
    assert helm_upgrade[:4] == ["helm", "upgrade", "--install", "recsys-serving"]
    assert "--atomic" in helm_upgrade
    assert helm_upgrade[helm_upgrade.index("--timeout") + 1] == "90s"
