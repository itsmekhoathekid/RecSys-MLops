from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml


def _documents(rendered: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]


def _render_observability(*extra_args: str) -> list[dict]:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "recsys-observability",
            "infra/helm/recsys-observability",
            "--namespace",
            "observability",
            *extra_args,
        ],
        text=True,
    )
    return _documents(rendered)


def _by_kind_name(docs: list[dict]) -> dict[tuple[str, str], dict]:
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}


def test_grafana_is_configured_for_public_gateway_origin():
    docs = _render_observability()
    deployment = _by_kind_name(docs)[("Deployment", "recsys-grafana")]
    env = {
        item["name"]: item["value"]
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert env["GF_SERVER_DOMAIN"] == "grafana.recsys.local"
    assert env["GF_SERVER_ROOT_URL"] == "http://grafana.recsys.local/"
