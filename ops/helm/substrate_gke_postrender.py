#!/usr/bin/env python3
"""Patch Substrate 0.0.6 to use unauthenticated public GKE OIDC discovery."""

from __future__ import annotations

import sys

IN_CLUSTER_CA = (
    "--client-jwt-ca-cert=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
)
SYSTEM_TRUST = "--client-jwt-ca-cert="


def main() -> None:
    """Replace only the known upstream ate-api-server JWT CA argument."""

    manifest = sys.stdin.read()
    occurrences = manifest.count(IN_CLUSTER_CA)
    if occurrences != 1:
        raise SystemExit(
            "expected exactly one Substrate client JWT CA argument, "
            f"found {occurrences}"
        )
    # An empty value makes ateapi use Go's system trust and, importantly, its
    # default HTTP transport. The v0.0.6 custom-CA transport injects the pod SA
    # bearer token; GKE's public discovery endpoint rejects that header with
    # HTTP 400.
    sys.stdout.write(manifest.replace(IN_CLUSTER_CA, SYSTEM_TRUST, 1))


if __name__ == "__main__":
    main()
