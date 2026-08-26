#!/usr/bin/env python3
"""Patch Substrate 0.0.6 for GKE OIDC and stable Valkey cluster discovery."""

from __future__ import annotations

import sys

IN_CLUSTER_CA = (
    "--client-jwt-ca-cert=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
)
SYSTEM_TRUST = "--client-jwt-ca-cert="
VALKEY_COMMAND = '        command: ["valkey-server", "/etc/valkey/valkey.conf"]'
VALKEY_COMMAND_WITH_POD_IP = '''        env:
        - name: POD_IP
          valueFrom:
            fieldRef:
              fieldPath: status.podIP
        command:
        - /bin/sh
        - -ec
        - exec valkey-server /etc/valkey/valkey.conf --cluster-announce-ip "${POD_IP}"'''


def main() -> None:
    """Apply the two fail-closed patches required by the pinned upstream chart."""

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
    manifest = manifest.replace(IN_CLUSTER_CA, SYSTEM_TRUST, 1)

    # The upstream Valkey StatefulSet persists nodes.conf on each PVC without
    # setting cluster-announce-ip. After a GKE node/pod recreation, each member
    # can therefore continue advertising its former pod IP even though the
    # cluster reports all slots as healthy. Substrate's cluster-aware client
    # then follows the stale topology and fails GetActor. Bind the announced
    # address to the current Downward API pod IP on every process start.
    valkey_command_occurrences = manifest.count(VALKEY_COMMAND)
    if valkey_command_occurrences != 1:
        raise SystemExit(
            "expected exactly one Substrate Valkey command, "
            f"found {valkey_command_occurrences}"
        )
    manifest = manifest.replace(
        VALKEY_COMMAND,
        VALKEY_COMMAND_WITH_POD_IP,
        1,
    )

    sys.stdout.write(manifest)


if __name__ == "__main__":
    main()
