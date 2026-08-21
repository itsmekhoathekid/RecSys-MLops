#!/usr/bin/env python3
"""Add the HPA selector field to the Terraform-owned kagent WorkerPool."""

from __future__ import annotations

import re
import sys

WORKER_POOL_KIND = re.compile(r"(?m)^kind: WorkerPool$")
WORKER_POOL_NAME = re.compile(r'(?m)^  name: ["\']?([^"\'\n]+)["\']?$')
SPEC_LINE = re.compile(r"(?m)^spec:$")


def patch_worker_pool(document: str) -> str:
    """Inject one selector derived from the rendered WorkerPool name."""

    name_match = WORKER_POOL_NAME.search(document)
    if name_match is None:
        raise SystemExit("WorkerPool metadata.name was not found")
    name = name_match.group(1)
    selector = f'  scaleSelector: "ate.dev/worker-pool={name}"'
    if "scaleSelector:" in document:
        raise SystemExit("WorkerPool already contains scaleSelector")
    spec_matches = list(SPEC_LINE.finditer(document))
    if len(spec_matches) != 1:
        raise SystemExit(
            f"expected exactly one WorkerPool spec block, found {len(spec_matches)}"
        )
    match = spec_matches[0]
    return document[: match.end()] + "\n" + selector + document[match.end() :]


def main() -> None:
    """Patch exactly one WorkerPool document and preserve all other manifests."""

    manifest = sys.stdin.read()
    documents = re.split(r"(?m)(^---\s*$)", manifest)
    patched = 0
    for index, document in enumerate(documents):
        if WORKER_POOL_KIND.search(document):
            documents[index] = patch_worker_pool(document)
            patched += 1
    if patched != 1:
        raise SystemExit(f"expected exactly one WorkerPool document, found {patched}")
    sys.stdout.write("".join(documents))


if __name__ == "__main__":
    main()
