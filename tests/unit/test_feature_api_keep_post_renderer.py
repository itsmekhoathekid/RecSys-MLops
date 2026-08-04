import subprocess
from pathlib import Path

import yaml


POST_RENDERER = (
    Path(__file__).parents[2]
    / "ops"
    / "migrations"
    / "helm-feature-api-keep"
    / "render.py"
)

PLUGIN_MANIFEST = POST_RENDERER.with_name("plugin.yaml")


def _render(source: str):
    result = subprocess.run(
        [str(POST_RENDERER)],
        input=source,
        text=True,
        capture_output=True,
        check=True,
    )
    return list(yaml.safe_load_all(result.stdout))


def test_post_renderer_protects_only_feature_api_resources():
    documents = _render(
        """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: recsys-online-feature-api
  annotations:
    checksum/config: abc
---
apiVersion: v1
kind: Secret
metadata:
  name: recsys-online-feature-api-registry
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: recsys-api-serving
"""
    )
    feature_annotations = documents[0]["metadata"]["annotations"]
    assert feature_annotations == {
        "checksum/config": "abc",
        "helm.sh/resource-policy": "keep",
    }
    assert documents[1]["metadata"]["annotations"] == {
        "helm.sh/resource-policy": "keep"
    }
    assert "annotations" not in documents[2]["metadata"]


def test_post_renderer_preserves_unknown_and_empty_documents():
    assert _render("plain text\n---\n") == ["plain text", None]


def test_helm4_post_renderer_plugin_is_versioned():
    manifest = yaml.safe_load(PLUGIN_MANIFEST.read_text(encoding="utf-8"))

    assert manifest["apiVersion"] == "v1"
    assert manifest["type"] == "postrenderer/v1"
    assert manifest["name"] == "recsys-feature-api-keep"
    assert manifest["runtimeConfig"]["platformCommand"] == [
        {"command": "${HELM_PLUGIN_DIR}/render.py"}
    ]
