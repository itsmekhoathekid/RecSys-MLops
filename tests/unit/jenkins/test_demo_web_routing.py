from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from jenkins.scripts.detect_changed_components import classify_paths


def test_demo_web_paths_select_only_the_demo_component() -> None:
    for path in (
        "apps/demo-web/frontend/src/App.tsx",
        "apps/demo-web/backend/app/main.py",
        "infra/helm/recsys-demo-web/templates/ingress.yaml",
        "jenkins/scripts/demo_web_smoke.sh",
        "jenkins/demo-web-rollback/Jenkinsfile",
        "tests/contract/test_demo_web_contracts.py",
    ):
        result = classify_paths([path])
        expected = ("demo_web", "ci_config") if path.startswith("jenkins/") else ("demo_web",)
        assert result.component_names == expected


def test_demo_security_and_gateway_contracts_include_the_demo_component() -> None:
    security = classify_paths(["infra/helm/recsys-security/templates/istio-authorization.yaml"])
    gateway = classify_paths(["tests/contract/test_gateway_contracts.py"])

    assert security.component_names == ("demo_web", "ci_config")
    assert gateway.component_names == ("api", "demo_web")
