from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


def _documents(rendered: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]


def _render_gateway(*extra_args: str) -> list[dict]:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "recsys-gateway",
            "infra/helm/recsys-gateway",
            "--namespace",
            "api-serving",
            *extra_args,
        ],
        text=True,
    )
    return _documents(rendered)


def _by_kind_name(docs: list[dict]) -> dict[tuple[str, str], dict]:
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}


def _backend(ingress: dict) -> dict:
    return ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]


def test_gateway_chart_renders_auth_rate_limits_and_backends_with_tls_disabled_by_default():
    docs = _render_gateway()
    by_kind_name = _by_kind_name(docs)

    assert ("ClusterIssuer", "letsencrypt-staging") not in by_kind_name
    assert ("Secret", "recsys-gateway-basic-auth") in by_kind_name
    secret_namespaces = {
        doc["metadata"]["namespace"]
        for doc in docs
        if doc["kind"] == "Secret" and doc["metadata"]["name"] == "recsys-gateway-basic-auth"
    }
    assert secret_namespaces == {"api-serving", "observability"}

    api = by_kind_name[("Ingress", "recsys-api-gateway")]
    assert api["metadata"]["namespace"] == "api-serving"
    assert api["spec"]["ingressClassName"] == "nginx"
    assert api["spec"]["rules"][0]["host"] == "api.recsys.local"
    assert "tls" not in api["spec"]
    assert _backend(api) == {"name": "recsys-api-serving", "port": {"number": 80}}

    annotations = api["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"
    assert annotations["nginx.ingress.kubernetes.io/auth-secret"] == "recsys-gateway-basic-auth"
    assert annotations["nginx.ingress.kubernetes.io/auth-realm"] == "RecSys Gateway"
    assert annotations["nginx.ingress.kubernetes.io/limit-rps"] == "5"
    assert annotations["nginx.ingress.kubernetes.io/limit-rpm"] == "120"
    assert annotations["nginx.ingress.kubernetes.io/limit-connections"] == "10"
    assert annotations["nginx.ingress.kubernetes.io/limit-req-status-code"] == "429"
    assert "nginx.ingress.kubernetes.io/force-ssl-redirect" not in annotations
    assert "cert-manager.io/cluster-issuer" not in annotations

    grafana = by_kind_name[("Ingress", "recsys-grafana-gateway")]
    assert grafana["metadata"]["namespace"] == "observability"
    assert grafana["spec"]["rules"][0]["host"] == "grafana.recsys.local"
    assert "tls" not in grafana["spec"]
    assert _backend(grafana) == {"name": "recsys-grafana", "port": {"number": 3000}}

    logs = by_kind_name[("Ingress", "recsys-logs-gateway")]
    assert logs["metadata"]["namespace"] == "observability"
    assert logs["spec"]["rules"][0]["host"] == "logs.recsys.local"
    assert "tls" not in logs["spec"]
    assert _backend(logs) == {"name": "recsys-loki", "port": {"number": 3100}}

    traces = by_kind_name[("Ingress", "recsys-traces-gateway")]
    assert traces["metadata"]["namespace"] == "observability"
    assert traces["spec"]["rules"][0]["host"] == "traces.recsys.local"
    assert "tls" not in traces["spec"]
    assert _backend(traces) == {"name": "recsys-tempo", "port": {"number": 3200}}


def test_gateway_chart_can_create_cert_manager_cluster_issuer():
    docs = _render_gateway(
        "--set",
        "tls.enabled=true",
        "--set",
        "tls.issuer.create=true",
        "--set",
        "tls.issuer.email=ops@example.com",
    )
    issuer = _by_kind_name(docs)[("ClusterIssuer", "letsencrypt-staging")]

    assert issuer["spec"]["acme"]["email"] == "ops@example.com"
    assert issuer["spec"]["acme"]["server"] == "https://acme-staging-v02.api.letsencrypt.org/directory"
    assert issuer["spec"]["acme"]["privateKeySecretRef"]["name"] == "letsencrypt-staging-account-key"
    assert issuer["spec"]["acme"]["solvers"] == [
        {"http01": {"ingress": {"ingressClassName": "nginx"}}}
    ]


def test_gateway_chart_can_disable_tls_for_local_http_smoke():
    docs = _render_gateway("--set", "tls.enabled=false")

    for ingress in [doc for doc in docs if doc["kind"] == "Ingress"]:
        annotations = ingress["metadata"].get("annotations", {})
        assert "tls" not in ingress["spec"]
        assert "nginx.ingress.kubernetes.io/force-ssl-redirect" not in annotations
        assert "cert-manager.io/cluster-issuer" not in annotations


def test_gateway_auth_secret_is_created_for_distinct_backend_namespaces():
    docs = _render_gateway(
        "--set",
        "logs.namespace=logs-ns",
        "--set",
        "traces.namespace=traces-ns",
    )
    secret_namespaces = {
        doc["metadata"]["namespace"]
        for doc in docs
        if doc["kind"] == "Secret" and doc["metadata"]["name"] == "recsys-gateway-basic-auth"
    }

    assert secret_namespaces == {"api-serving", "observability", "logs-ns", "traces-ns"}


def test_makefile_exposes_gateway_targets_and_domain_overrides():
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert ".PHONY: gateway-install-controller" in makefile
    assert ".PHONY: gateway-create-auth" in makefile
    assert ".PHONY: gateway-install" in makefile
    assert ".PHONY: gateway-smoke" in makefile
    assert "--set controller.config.limit-req-status-code=429" in makefile
    assert "--set controller.config.limit-conn-status-code=429" in makefile
    assert "GATEWAY_AUTH_USER" in makefile
    assert "GATEWAY_AUTH_PASSWORD" in makefile
    assert "seq 1 100" in makefile
    assert "--set api.host=$(GATEWAY_API_HOST)" in makefile
    assert "--set grafana.host=$(GATEWAY_GRAFANA_HOST)" in makefile
    assert "--set logs.host=$(GATEWAY_LOGS_HOST)" in makefile
    assert "--set traces.host=$(GATEWAY_TRACES_HOST)" in makefile
