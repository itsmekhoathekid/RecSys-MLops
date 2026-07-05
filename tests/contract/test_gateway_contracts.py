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


def _paths(ingress: dict) -> dict[str, dict]:
    return {
        path["path"]: path
        for path in ingress["spec"]["rules"][0]["http"]["paths"]
    }


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

    feature_api = by_kind_name[("Ingress", "recsys-online-feature-api-gateway")]
    assert feature_api["metadata"]["namespace"] == "api-serving"
    assert feature_api["spec"]["ingressClassName"] == "nginx"
    assert feature_api["spec"]["rules"][0]["host"] == "features.recsys.local"
    assert "tls" not in feature_api["spec"]
    assert _backend(feature_api) == {
        "name": "recsys-online-feature-api",
        "port": {"number": 80},
    }
    feature_annotations = feature_api["metadata"]["annotations"]
    assert feature_annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"
    assert (
        feature_annotations["nginx.ingress.kubernetes.io/auth-secret"]
        == "recsys-gateway-basic-auth"
    )
    assert feature_annotations["nginx.ingress.kubernetes.io/upstream-vhost"] == (
        "recsys-online-feature-api.api-serving.svc.cluster.local"
    )
    assert feature_annotations["nginx.ingress.kubernetes.io/limit-rps"] == "5"
    assert feature_annotations["nginx.ingress.kubernetes.io/limit-rpm"] == "120"
    assert feature_annotations["nginx.ingress.kubernetes.io/limit-connections"] == "10"
    assert (
        feature_annotations["nginx.ingress.kubernetes.io/limit-req-status-code"]
        == "429"
    )

    grafana = by_kind_name[("Ingress", "recsys-grafana-gateway")]
    assert grafana["metadata"]["namespace"] == "observability"
    assert grafana["spec"]["rules"][0]["host"] == "grafana.recsys.local"
    assert "tls" not in grafana["spec"]
    assert _backend(grafana) == {"name": "recsys-grafana", "port": {"number": 3000}}
    assert (
        "nginx.ingress.kubernetes.io/upstream-vhost"
        not in grafana["metadata"]["annotations"]
    )
    grafana_annotations = grafana["metadata"]["annotations"]
    assert grafana_annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"
    assert grafana_annotations["nginx.ingress.kubernetes.io/limit-rps"] == "5"
    assert grafana_annotations["nginx.ingress.kubernetes.io/limit-rpm"] == "120"
    assert grafana_annotations["nginx.ingress.kubernetes.io/limit-connections"] == "10"
    assert grafana_annotations["nginx.ingress.kubernetes.io/limit-req-status-code"] == "429"

    grafana_service_entry = by_kind_name[
        ("ServiceEntry", "recsys-grafana-gateway-service-entry")
    ]
    assert grafana_service_entry["metadata"]["namespace"] == "observability"
    assert grafana_service_entry["spec"]["hosts"] == ["grafana.recsys.local"]
    assert grafana_service_entry["spec"]["ports"] == [
        {"name": "http", "number": 3000, "protocol": "HTTP"}
    ]
    assert grafana_service_entry["spec"]["endpoints"] == [
        {
            "address": "recsys-grafana.observability.svc.cluster.local",
            "ports": {"http": 3000},
        }
    ]

    grafana_mesh_route = by_kind_name[
        ("VirtualService", "recsys-grafana-gateway-mesh-route")
    ]
    assert grafana_mesh_route["metadata"]["namespace"] == "observability"
    assert grafana_mesh_route["spec"]["hosts"] == ["grafana.recsys.local"]
    assert grafana_mesh_route["spec"]["gateways"] == ["mesh"]
    assert grafana_mesh_route["spec"]["http"][0]["route"][0]["destination"] == {
        "host": "recsys-grafana.observability.svc.cluster.local",
        "port": {"number": 3000},
    }

    logs = by_kind_name[("Ingress", "recsys-logs-gateway")]
    assert logs["metadata"]["namespace"] == "observability"
    assert logs["spec"]["rules"][0]["host"] == "logs.recsys.local"
    assert "tls" not in logs["spec"]
    logs_paths = _paths(logs)
    assert logs_paths["/loki"]["pathType"] == "Prefix"
    assert logs_paths["/loki"]["backend"]["service"] == {
        "name": "recsys-loki",
        "port": {"number": 3100},
    }
    assert logs_paths["/ready"]["pathType"] == "Exact"
    assert logs_paths["/ready"]["backend"]["service"] == {
        "name": "recsys-loki",
        "port": {"number": 3100},
    }
    assert logs_paths["/metrics"]["pathType"] == "Exact"
    assert logs_paths["/metrics"]["backend"]["service"] == {
        "name": "recsys-loki",
        "port": {"number": 3100},
    }
    assert "/" not in logs_paths
    logs_annotations = logs["metadata"]["annotations"]
    assert logs_annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"
    assert logs_annotations["nginx.ingress.kubernetes.io/upstream-vhost"] == (
        "recsys-loki.observability.svc.cluster.local"
    )
    assert logs_annotations["nginx.ingress.kubernetes.io/limit-rps"] == "5"
    assert logs_annotations["nginx.ingress.kubernetes.io/limit-rpm"] == "120"
    assert logs_annotations["nginx.ingress.kubernetes.io/limit-connections"] == "10"
    assert logs_annotations["nginx.ingress.kubernetes.io/limit-req-status-code"] == "429"

    logs_root_redirect = by_kind_name[("Ingress", "recsys-logs-root-redirect")]
    assert logs_root_redirect["metadata"]["namespace"] == "observability"
    assert logs_root_redirect["spec"]["rules"][0]["host"] == "logs.recsys.local"
    assert _paths(logs_root_redirect)["/"]["pathType"] == "Exact"
    assert _backend(logs_root_redirect) == {
        "name": "recsys-loki",
        "port": {"number": 3100},
    }
    redirect_annotations = logs_root_redirect["metadata"]["annotations"]
    assert redirect_annotations["nginx.ingress.kubernetes.io/permanent-redirect"] == (
        "http://grafana.recsys.local/d/recsys-logs/logs-overview"
    )

    traces = by_kind_name[("Ingress", "recsys-traces-gateway")]
    assert traces["metadata"]["namespace"] == "observability"
    assert traces["spec"]["rules"][0]["host"] == "traces.recsys.local"
    assert "tls" not in traces["spec"]
    assert _backend(traces) == {"name": "recsys-tempo", "port": {"number": 3200}}
    traces_annotations = traces["metadata"]["annotations"]
    assert traces_annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"
    assert traces_annotations["nginx.ingress.kubernetes.io/limit-rps"] == "5"
    assert traces_annotations["nginx.ingress.kubernetes.io/limit-rpm"] == "120"
    assert traces_annotations["nginx.ingress.kubernetes.io/limit-connections"] == "10"
    assert traces_annotations["nginx.ingress.kubernetes.io/limit-req-status-code"] == "429"


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
    assert (
        issuer["spec"]["acme"]["server"]
        == "https://acme-staging-v02.api.letsencrypt.org/directory"
    )
    assert (
        issuer["spec"]["acme"]["privateKeySecretRef"]["name"]
        == "letsencrypt-staging-account-key"
    )
    assert issuer["spec"]["acme"]["solvers"] == [
        {"http01": {"ingress": {"ingressClassName": "nginx"}}}
    ]
    feature_api = _by_kind_name(docs)[("Ingress", "recsys-online-feature-api-gateway")]
    assert feature_api["spec"]["tls"] == [
        {"hosts": ["features.recsys.local"], "secretName": "recsys-feature-api-tls"}
    ]
    assert (
        feature_api["metadata"]["annotations"][
            "nginx.ingress.kubernetes.io/force-ssl-redirect"
        ]
        == "true"
    )


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
        "featureApi.namespace=feature-ns",
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

    assert secret_namespaces == {"api-serving", "feature-ns", "observability", "logs-ns", "traces-ns"}


def test_gateway_grafana_upstream_host_can_be_overridden_when_needed():
    docs = _render_gateway("--set", "grafana.upstreamHost=grafana.internal.example")
    grafana = _by_kind_name(docs)[("Ingress", "recsys-grafana-gateway")]

    assert (
        grafana["metadata"]["annotations"][
            "nginx.ingress.kubernetes.io/upstream-vhost"
        ]
        == "grafana.internal.example"
    )


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
    assert "GATEWAY_AUTH_SECRET" in makefile
    assert 'if [ -f "$(GATEWAY_AUTH_FILE)" ]' in makefile
    assert "--set-file auth.htpasswd=$(GATEWAY_AUTH_FILE)" in makefile
    assert "seq 1 100" in makefile
    assert "--set api.host=$(GATEWAY_API_HOST)" in makefile
    assert "--set featureApi.host=$(GATEWAY_FEATURE_API_HOST)" in makefile
    assert "--set grafana.host=$(GATEWAY_GRAFANA_HOST)" in makefile
    assert "--set logs.host=$(GATEWAY_LOGS_HOST)" in makefile
    assert "--set traces.host=$(GATEWAY_TRACES_HOST)" in makefile
