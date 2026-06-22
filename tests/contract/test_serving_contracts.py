from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


sys.path.append(str(Path("jenkins/scripts").resolve()))

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
