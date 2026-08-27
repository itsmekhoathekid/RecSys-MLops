from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
POSTRENDER = ROOT / "ops/helm/substrate_gke_postrender.py"

MANIFEST = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: ate-api-server-deployment
spec:
  template:
    spec:
      containers:
      - name: ate-api-server
        args:
        - --client-jwt-ca-cert=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: valkey-cluster
spec:
  template:
    spec:
      containers:
      - name: valkey
        command: ["valkey-server", "/etc/valkey/valkey.conf"]
"""

AGENTGATEWAY_MANIFEST = """---
apiVersion: v1
kind: ConfigMap
metadata:
  name: atenet-router-agentgateway-config
data:
  config.yaml: |
    # yaml-language-server: $schema=https://agentgateway.dev/schema/config
    config:
      # Actor sandboxes behind a worker IP are replaced between requests. Do
      # not retain an idle connection that may belong to the previous actor.
      backend:
        poolMaxSize: 0

    gateways:
      http:
        port: 8080
        protocol: HTTP
      https:
        port: 8443
        protocol: HTTPS
        tls:
          cert: /run/servicedns.podcert.ate.dev/credential-bundle.pem
          key: /run/servicedns.podcert.ate.dev/credential-bundle.pem

    routes:
    - name: substrate-actors
      gateways:
      - http
      - https
      matches:
      - path:
          pathPrefix: /
      policies:
        extProc:
          host: 127.0.0.1:50051
          failureMode: failClosed
          processingOptions:
            requestHeaderMode: send
            responseHeaderMode: skip
            requestBodyMode: none
            responseBodyMode: none
            requestTrailerMode: skip
            responseTrailerMode: skip
      backends:
      - dynamic: {}
        policies:
          backendTLS:
            cert: /run/podidentity.podcert.ate.dev/credential-bundle.pem
            key: /run/podidentity.podcert.ate.dev/credential-bundle.pem
            root: /run/podidentity.podcert.ate.dev/trust-bundle.pem
            insecureHost: true
"""

ATELET_MANIFEST = """---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: atelet
spec:
  template:
    spec:
      containers:
      - name: atelet
        args:
        - --client-ca-certs=/run/podidentity.podcert.ate.dev/trust-bundle.pem
        securityContext:
          privileged: true
        volumeMounts:
        - name: run-ateom
          mountPath: /var/lib/ateom-gvisor
        - name: podidentity
          mountPath: /run/podidentity.podcert.ate.dev
          readOnly: true
      volumes:
      - name: run-ateom
        hostPath:
          path: /var/lib/ateom-gvisor
          type: DirectoryOrCreate
      - name: podidentity
        projected:
          sources:
          - podCertificate:
              signerName: podidentity.podcert.ate.dev/identity
              keyType: ECDSAP256
              credentialBundlePath: credential-bundle.pem
          - clusterTrustBundle:
              signerName: podidentity.podcert.ate.dev/identity
              labelSelector:
                matchLabels:
                  podcert.ate.dev/canarying: live
              path: trust-bundle.pem
"""


def run_postrenderer(
    manifest: str, profile: str = "gke-public-oidc-v1"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(POSTRENDER), profile],
        cwd=ROOT,
        input=manifest,
        text=True,
        capture_output=True,
        check=False,
    )


def test_gke_postrenderer_patches_oidc_and_valkey_announce_ip() -> None:
    completed = run_postrenderer(MANIFEST)

    assert completed.returncode == 0, completed.stderr
    assert "--client-jwt-ca-cert=\n" in completed.stdout
    assert "fieldPath: status.podIP" in completed.stdout
    assert "--cluster-announce-ip \"${POD_IP}\"" in completed.stdout


def test_gke_postrenderer_fails_closed_without_valkey_command() -> None:
    completed = run_postrenderer(
        MANIFEST.replace(
            'command: ["valkey-server", "/etc/valkey/valkey.conf"]',
            'command: ["valkey-server"]',
        )
    )

    assert completed.returncode != 0
    assert "expected exactly one Substrate Valkey command" in completed.stderr


def test_gke_mtls_0011_postrenderer_uses_stable_dns_and_discovery_rbac() -> None:
    manifest = MANIFEST.replace(
        "        - --client-jwt-ca-cert=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt\n",
        "",
    ).replace(
        "---\napiVersion: apps/v1",
        '''---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
rules:
- apiGroups: ["ate.dev"]
  resources: ["actortemplates"]
  verbs: ["get", "watch", "list"]
# Secret reads for env source resolution
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: atelet-role
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "watch", "list"]
---
apiVersion: apps/v1''',
        1,
    )

    completed = run_postrenderer(
        manifest + ATELET_MANIFEST + AGENTGATEWAY_MANIFEST, "gke-mtls-0011-v1"
    )

    assert completed.returncode == 0, completed.stderr
    assert "--cluster-announce-hostname" in completed.stdout
    assert "--cluster-preferred-endpoint-type hostname" in completed.stdout
    assert completed.stdout.count('resources: ["csidriverconfigs"]') == 2
    assert completed.stdout.count('resources: ["storageclasses"]') == 2
    assert "--ateapi-address=dns:///api.ate-system.svc:443" in completed.stdout
    assert "- name: servicedns-ca" in completed.stdout
    assert "signerName: servicedns.podcert.ate.dev/identity" in completed.stdout
    assert "    binds:\n" in completed.stdout
    assert "    gateways:\n" not in completed.stdout
    assert "/run/servicedns.podcert.ate.dev/cert.pem" in completed.stdout


def test_gke_mtls_0011_postrenderer_rejects_jwt_manifest() -> None:
    completed = run_postrenderer(MANIFEST, "gke-mtls-0011-v1")

    assert completed.returncode != 0
    assert "unexpectedly contains the JWT CA argument" in completed.stderr
