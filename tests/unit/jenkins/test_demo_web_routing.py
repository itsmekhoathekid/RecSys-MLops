from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from jenkins.python.change_detection.detector import (  # noqa: E402
    ChangedFile,
    detect_changed_components,
)


def detect(path: str):
    return detect_changed_components([ChangedFile("M", path)])


def test_demo_web_paths_select_only_the_demo_component() -> None:
    for path in (
        "apps/demo-web/frontend/src/App.tsx",
        "apps/demo-web/backend/app/main.py",
        "infra/helm/recsys-demo-web/templates/ingress.yaml",
        "jenkins/scripts/test/demo_web_smoke.sh",
        "tests/contract/test_demo_web_contracts.py",
    ):
        result = detect(path)
        assert result.component_names == ("demo_web",)
        if path.startswith(("jenkins/", "infra/helm/", "tests/contract/")):
            assert result.flags["RUN_CI_CONFIG"] is True


def test_demo_security_and_gateway_contracts_include_the_demo_component() -> None:
    security = detect("infra/helm/recsys-security/templates/istio-authorization.yaml")
    gateway = detect("tests/contract/test_gateway_contracts.py")

    assert security.component_names == ("demo_web",)
    assert security.flags["RUN_CI_CONFIG"] is True
    assert gateway.component_names == ("api", "demo_web")
