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


def run_postrenderer(manifest: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(POSTRENDER)],
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
