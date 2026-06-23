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
    kserve_http_scaledobject = by_kind_name[("HTTPScaledObject", "recsys-bst-triton-http")]
    assert kserve_http_scaledobject["metadata"]["namespace"] == "kserve-triton-inference"
    assert kserve_http_scaledobject["metadata"]["annotations"][
        "httpscaledobject.keda.sh/skip-scaledobject-creation"
    ] == "true"
    assert kserve_http_scaledobject["spec"]["hosts"] == ["recsys-bst-triton.local"]
    assert kserve_http_scaledobject["spec"]["scaleTargetRef"]["name"] == "recsys-bst-triton-predictor"
    assert kserve_http_scaledobject["spec"]["scaleTargetRef"]["service"] == "recsys-bst-triton-http"
    assert kserve_http_scaledobject["spec"]["scaleTargetRef"]["port"] == 80
    assert kserve_http_scaledobject["spec"]["scalingMetric"]["requestRate"]["targetValue"] == 3
    kserve_keda_scaledobject = by_kind_name[("ScaledObject", "recsys-bst-triton-http")]
    assert kserve_keda_scaledobject["metadata"]["namespace"] == "kserve-triton-inference"
    assert kserve_keda_scaledobject["metadata"]["annotations"][
        "scaledobject.keda.sh/transfer-hpa-ownership"
    ] == "true"
    assert (
        kserve_keda_scaledobject["spec"]["advanced"]["horizontalPodAutoscalerConfig"]["name"]
        == "recsys-bst-triton-predictor"
    )
    assert kserve_keda_scaledobject["spec"]["triggers"][0]["type"] == "external-push"
    kserve_http_service = by_kind_name[("Service", "recsys-bst-triton-http")]
    assert kserve_http_service["metadata"]["namespace"] == "kserve-triton-inference"
    assert kserve_http_service["spec"]["selector"] == {"app": "isvc.recsys-bst-triton-predictor"}
    assert kserve_http_service["spec"]["ports"][0]["targetPort"] == 8080


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
    assert ("Service", "recsys-bst-triton-http") in by_kind_name
    assert ("HTTPScaledObject", "recsys-api-serving-http") in by_kind_name
    assert ("HTTPScaledObject", "recsys-bst-triton-http") in by_kind_name
    assert ("InferenceService", "recsys-bst-triton") not in by_kind_name
    assert ("ClusterServingRuntime", "kserve-tritonserver") not in by_kind_name


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
