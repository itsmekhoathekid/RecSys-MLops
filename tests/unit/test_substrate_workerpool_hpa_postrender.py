from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRD_POSTRENDER = ROOT / "ops/helm/substrate_crds_hpa_postrender.py"
KAGENT_POSTRENDER = ROOT / "ops/helm/kagent_workerpool_hpa_postrender.py"

CRD_MANIFEST = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: workerpools.ate.dev
spec:
  versions:
  - schema:
      openAPIV3Schema:
        properties:
          spec:
            properties:
              replicas:
                description: Replicas is the number of worker pods to run.
                format: int32
                minimum: 0
                type: integer
    subresources:
      scale:
        specReplicasPath: .spec.replicas
        statusReplicasPath: .status.replicas
      status: {}
"""

KAGENT_MANIFEST = """apiVersion: v1
kind: Service
metadata:
  name: kagent
---
apiVersion: ate.dev/v1alpha1
kind: WorkerPool
metadata:
  name: "recsys-context-sandbox-pool"
  namespace: kagent
spec:
  replicas: 2
  ateomImage: "ghcr.io/kagent-dev/substrate/ateom-gvisor:v0.0.6"
"""


def run_postrenderer(path: Path, manifest: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(path)],
        cwd=ROOT,
        input=manifest,
        text=True,
        capture_output=True,
        check=False,
    )


def test_crd_postrenderer_adds_hpa_selector_contract() -> None:
    completed = run_postrenderer(CRD_POSTRENDER, CRD_MANIFEST)

    assert completed.returncode == 0, completed.stderr
    assert "labelSelectorPath: .spec.scaleSelector" in completed.stdout
    assert "scaleSelector:" in completed.stdout
    assert completed.stdout.count("specReplicasPath: .spec.replicas") == 1


def test_crd_postrenderer_fails_closed_when_upstream_shape_changes() -> None:
    completed = run_postrenderer(
        CRD_POSTRENDER,
        CRD_MANIFEST.replace("statusReplicasPath", "observedReplicasPath"),
    )

    assert completed.returncode != 0
    assert "expected exactly one WorkerPool scale subresource" in completed.stderr


def test_kagent_postrenderer_adds_selector_derived_from_pool_name() -> None:
    completed = run_postrenderer(KAGENT_POSTRENDER, KAGENT_MANIFEST)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.count("scaleSelector:") == 1
    assert (
        'scaleSelector: "ate.dev/worker-pool=recsys-context-sandbox-pool"'
        in completed.stdout
    )
    assert "kind: Service" in completed.stdout


def test_kagent_postrenderer_requires_exactly_one_worker_pool() -> None:
    completed = run_postrenderer(KAGENT_POSTRENDER, KAGENT_MANIFEST * 2)

    assert completed.returncode != 0
    assert "expected exactly one WorkerPool document" in completed.stderr
