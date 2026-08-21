#!/usr/bin/env python3
"""Backport the WorkerPool scale selector missing from Substrate 0.0.6."""

from __future__ import annotations

import sys

SPEC_REPLICAS_SCHEMA = """              replicas:
                description: Replicas is the number of worker pods to run.
                format: int32
                minimum: 0
                type: integer"""
SPEC_SELECTOR_SCHEMA = """              scaleSelector:
                description: Label selector returned by the scale subresource for HPA.
                minLength: 1
                type: string
"""
SCALE_SUBRESOURCE = """      scale:
        specReplicasPath: .spec.replicas
        statusReplicasPath: .status.replicas"""
SCALE_SUBRESOURCE_WITH_SELECTOR = """      scale:
        labelSelectorPath: .spec.scaleSelector
        specReplicasPath: .spec.replicas
        statusReplicasPath: .status.replicas"""


def replace_once(manifest: str, old: str, new: str, label: str) -> str:
    """Replace exactly one known upstream fragment or fail closed."""

    occurrences = manifest.count(old)
    if occurrences != 1:
        raise SystemExit(f"expected exactly one {label}, found {occurrences}")
    return manifest.replace(old, new, 1)


def main() -> None:
    """Add an additive spec selector and wire it to the CRD scale endpoint."""

    manifest = sys.stdin.read()
    manifest = replace_once(
        manifest,
        SPEC_REPLICAS_SCHEMA,
        SPEC_SELECTOR_SCHEMA + SPEC_REPLICAS_SCHEMA,
        "WorkerPool spec replicas schema",
    )
    manifest = replace_once(
        manifest,
        SCALE_SUBRESOURCE,
        SCALE_SUBRESOURCE_WITH_SELECTOR,
        "WorkerPool scale subresource",
    )
    sys.stdout.write(manifest)


if __name__ == "__main__":
    main()
