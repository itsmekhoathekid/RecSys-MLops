from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jenkins.scripts.detect_changed_components import classify_paths


def rendered_chart() -> list[dict]:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    output = subprocess.check_output(
        [
            "helm",
            "template",
            "recsys-demo-web",
            "infra/helm/recsys-demo-web",
            "-f",
            "infra/helm/recsys-demo-web/values-gcp.yaml",
            "--namespace",
            "api-serving",
        ],
        cwd=ROOT,
        text=True,
    )
    return [document for document in yaml.safe_load_all(output) if isinstance(document, dict)]


def by_kind_name(documents: list[dict]) -> dict[tuple[str, str], dict]:
    return {(document["kind"], document["metadata"]["name"]): document for document in documents}


def test_gcp_chart_renders_two_hardened_workloads_and_root_tls_ingress() -> None:
    documents = by_kind_name(rendered_chart())
    frontend = documents[("Deployment", "recsys-demo-web")]
    backend = documents[("Deployment", "recsys-demo-api")]
    ingress = documents[("Ingress", "recsys-demo-web")]
    api_ingress = documents[("Ingress", "recsys-demo-api")]
    pod_monitoring = documents[("PodMonitoring", "recsys-demo-api")]

    assert frontend["spec"]["replicas"] == 2
    assert backend["spec"]["replicas"] == 2
    assert frontend["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    assert backend["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True
    assert backend["spec"]["template"]["spec"]["containers"][0]["envFrom"][1]["secretRef"]["name"] == (
        "recsys-demo-web-db"
    )
    assert ingress["spec"]["rules"][0]["host"] == "recsys-mlops.site"
    assert ingress["spec"]["tls"] == [{"hosts": ["recsys-mlops.site"], "secretName": "recsys-web-tls"}]
    assert ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/auth-secret"] == (
        "recsys-gateway-basic-auth"
    )
    assert ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/service-upstream"] == "true"
    assert ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/upstream-vhost"] == (
        "recsys-demo-web.api-serving.svc.cluster.local"
    )
    assert api_ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/upstream-vhost"] == (
        "recsys-demo-api.api-serving.svc.cluster.local"
    )
    assert api_ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/auth-secret"] == (
        "recsys-gateway-basic-auth"
    )
    assert ingress["metadata"]["annotations"]["cert-manager.io/cluster-issuer"] == "letsencrypt-prod"
    assert pod_monitoring["apiVersion"] == "monitoring.googleapis.com/v1"
    assert pod_monitoring["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "recsys-demo-api"
    }
    assert pod_monitoring["spec"]["endpoints"] == [
        {"port": "http", "path": "/metrics", "interval": "30s"}
    ]
    assert all(document.get("kind") != "ServiceMonitor" for document in rendered_chart())
    paths = {
        path["path"]: path["backend"]["service"]["name"]
        for path in ingress["spec"]["rules"][0]["http"]["paths"]
    }
    api_paths = {
        path["path"]: path["backend"]["service"]["name"]
        for path in api_ingress["spec"]["rules"][0]["http"]["paths"]
    }
    assert paths == {"/": "recsys-demo-web"}
    assert api_paths == {
        "/api": "recsys-demo-api",
        "/healthz": "recsys-demo-api",
        "/ready": "recsys-demo-api",
    }


def test_external_secret_exposes_only_source_postgres_credentials() -> None:
    external_secret = by_kind_name(rendered_chart())[("ExternalSecret", "recsys-demo-web-db")]
    assert external_secret["spec"]["secretStoreRef"] == {
        "kind": "ClusterSecretStore",
        "name": "recsys-central-secrets",
    }
    assert external_secret["spec"]["data"] == [
        {"secretKey": "POSTGRES_USER", "remoteRef": {"key": "data-platform", "property": "POSTGRES_USER"}},
        {
            "secretKey": "POSTGRES_PASSWORD",
            "remoteRef": {"key": "data-platform", "property": "POSTGRES_PASSWORD"},
        },
    ]


def test_demo_web_paths_route_to_the_dedicated_jenkins_component() -> None:
    result = classify_paths(
        [
            "apps/demo-web/frontend/src/App.tsx",
            "apps/demo-web/backend/app/main.py",
            "infra/helm/recsys-demo-web/values-gcp.yaml",
            "jenkins/demo-web-rollback/Jenkinsfile",
        ]
    )
    assert result.component_names == ("demo_web", "ci_config")
    assert result.unmapped_paths == ()


def test_jenkins_seeds_demo_view_cicd_and_rollback_jobs() -> None:
    seed = (ROOT / "infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml").read_text(encoding="utf-8")
    deploy = (ROOT / "jenkins/scripts/component_deploy.sh").read_text(encoding="utf-8")
    build = (ROOT / "jenkins/scripts/component_build_publish.sh").read_text(encoding="utf-8")

    assert "10 Recommendation Web App" in seed
    assert "RecSys-Recommendation-Web-CICD" in seed
    assert "RecSys-Recommendation-Web-Rollback" in seed
    assert "jenkins/demo-web-rollback/Jenkinsfile" in seed
    assert "--atomic" in deploy
    assert "demo_web_smoke.sh" in deploy
    assert "migrate_demo_ingress_split" in deploy
    assert "ingress/recsys-demo-api" in deploy
    assert "nginx.ingress.kubernetes.io~1upstream-vhost" in deploy
    assert 'with_file_lock "/tmp/recsys-demo-web-helm.lock" deploy_demo_web_unlocked' in deploy
    smoke = (ROOT / "jenkins/scripts/demo_web_smoke.sh").read_text(encoding="utf-8")
    assert 'kubectl exec -n "${namespace}" deploy/recsys-demo-api -c backend' in smoke
    assert ".demo-web/**/*" in (ROOT / "Jenkinsfile").read_text(encoding="utf-8")
    assert 'build_and_optionally_push "recsys-demo-api"' in build
    assert 'build_and_optionally_push "recsys-demo-web"' in build
