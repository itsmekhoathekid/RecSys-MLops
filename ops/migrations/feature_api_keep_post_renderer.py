#!/usr/bin/env python3
"""Protect legacy Feature API objects during the Helm ownership transfer."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Any, TextIO

import yaml


FEATURE_RESOURCES = {
    ("ConfigMap", "recsys-online-feature-api"),
    ("Deployment", "recsys-online-feature-api"),
    ("ScaledObject", "recsys-online-feature-api-prometheus"),
    ("Secret", "recsys-online-feature-api-registry"),
    ("Service", "recsys-online-feature-api"),
    ("ServiceMonitor", "recsys-online-feature-api"),
}


def protect_feature_resources(documents: Iterable[Any]) -> list[Any]:
    rendered: list[Any] = []
    for document in documents:
        if not isinstance(document, dict):
            rendered.append(document)
            continue

        metadata = document.get("metadata")
        identity = (
            document.get("kind"),
            metadata.get("name") if isinstance(metadata, dict) else None,
        )
        if identity in FEATURE_RESOURCES:
            annotations = metadata.setdefault("annotations", {})
            annotations["helm.sh/resource-policy"] = "keep"
        rendered.append(document)
    return rendered


def render(source: TextIO, target: TextIO) -> None:
    documents = protect_feature_resources(yaml.safe_load_all(source))
    yaml.safe_dump_all(documents, target, sort_keys=False)


if __name__ == "__main__":
    render(sys.stdin, sys.stdout)
