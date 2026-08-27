#!/usr/bin/env python3
"""Apply fail-closed GKE compatibility patches to pinned Substrate charts."""

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
VALKEY_STABLE_COMMAND = '''        command:
        - /bin/sh
        - -c
        - >-
          exec valkey-server /etc/valkey/valkey.conf
          --cluster-announce-hostname
          "${POD_NAME}.valkey-cluster-service.${POD_NAMESPACE}.svc"
          --cluster-preferred-endpoint-type hostname
        env:
        - name: POD_NAME
          valueFrom:
            fieldRef:
              fieldPath: metadata.name
        - name: POD_NAMESPACE
          valueFrom:
            fieldRef:
              fieldPath: metadata.namespace'''
ATE_API_ROLE_ANCHOR = "# Secret reads for env source resolution"
ATE_API_ROLE_WITH_DISCOVERY = '''- apiGroups: ["ate.dev"]
  resources: ["csidriverconfigs"]
  verbs: ["get", "watch", "list"]
- apiGroups: ["storage.k8s.io"]
  resources: ["storageclasses"]
  verbs: ["get", "watch", "list"]
# Secret reads for env source resolution'''
ATELET_ROLE_ANCHOR = '''  name: atelet-role
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "watch", "list"]'''
ATELET_ROLE_WITH_DISCOVERY = '''  name: atelet-role
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "watch", "list"]
- apiGroups: ["ate.dev"]
  resources: ["csidriverconfigs"]
  verbs: ["get", "watch", "list"]
- apiGroups: ["storage.k8s.io"]
  resources: ["storageclasses"]
  verbs: ["get", "watch", "list"]'''
ATELET_CLIENT_ARGS_ANCHOR = '''        - --client-ca-certs=/run/podidentity.podcert.ate.dev/trust-bundle.pem
        securityContext:'''
ATELET_CLIENT_ARGS_WITH_DNS = '''        - --client-ca-certs=/run/podidentity.podcert.ate.dev/trust-bundle.pem
        - --ateapi-address=dns:///api.ate-system.svc:443
        - --ateapi-ca-file=/run/servicedns.podcert.ate.dev/trust-bundle.pem
        securityContext:'''
ATELET_VOLUMES_ANCHOR = '''        volumeMounts:
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
              path: trust-bundle.pem'''
ATELET_VOLUMES_WITH_SERVICEDNS = '''        volumeMounts:
        - name: run-ateom
          mountPath: /var/lib/ateom-gvisor
        - name: podidentity
          mountPath: /run/podidentity.podcert.ate.dev
          readOnly: true
        - name: servicedns-ca
          mountPath: /run/servicedns.podcert.ate.dev
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
      - name: servicedns-ca
        projected:
          sources:
          - clusterTrustBundle:
              signerName: servicedns.podcert.ate.dev/identity
              labelSelector:
                matchLabels:
                  podcert.ate.dev/canarying: live
              path: trust-bundle.pem'''
AGENTGATEWAY_GATEWAYS_CONFIG = '''    # yaml-language-server: $schema=https://agentgateway.dev/schema/config
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
            insecureHost: true'''
AGENTGATEWAY_BINDS_CONFIG = '''    # The agentgateway image bundled by Substrate 0.0.11 accepts the established
    # binds schema but rejects the chart's gateways/routes schema at startup.
    config:
      backend:
        poolMaxSize: 0
    binds:
    - port: 8080
      listeners:
      - name: http
        protocol: HTTP
        routes:
        - name: substrate-http
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
    - port: 8443
      listeners:
      - name: https
        protocol: HTTPS
        tls:
          cert: /run/servicedns.podcert.ate.dev/cert.pem
          key: /run/servicedns.podcert.ate.dev/key.pem
        routes:
        - name: substrate-https
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
                insecureHost: true'''


def patch_exactly_once(manifest: str, old: str, new: str, label: str) -> str:
    """Replace one required chart fragment and fail if upstream drifted."""

    occurrences = manifest.count(old)
    if occurrences != 1:
        raise SystemExit(f"expected exactly one Substrate {label}, found {occurrences}")
    return manifest.replace(old, new, 1)


def main() -> None:
    """Apply the compatibility profile selected by the Terraform release."""

    manifest = sys.stdin.read()
    profile = sys.argv[1] if len(sys.argv) > 1 else "gke-public-oidc-v1"

    if profile == "gke-public-oidc-v1":
        # Substrate 0.0.6: use system trust for GKE's public issuer and bind
        # persisted Valkey topology to the current pod IP.
        manifest = patch_exactly_once(
            manifest, IN_CLUSTER_CA, SYSTEM_TRUST, "client JWT CA argument"
        )
        manifest = patch_exactly_once(
            manifest, VALKEY_COMMAND, VALKEY_COMMAND_WITH_POD_IP, "Valkey command"
        )
    elif profile == "gke-mtls-0011-v1":
        # Substrate 0.0.11 mTLS has no client-jwt-ca-cert argument. StatefulSet
        # DNS survives pod recreation, unlike persisted pod IPs.
        if IN_CLUSTER_CA in manifest:
            raise SystemExit(
                "Substrate 0.0.11 mTLS manifest unexpectedly contains the JWT CA argument"
            )
        manifest = patch_exactly_once(
            manifest, VALKEY_COMMAND, VALKEY_STABLE_COMMAND, "Valkey command"
        )
        # ateapi 0.0.11 starts these informers before its health listener; the
        # upstream chart omits their read-only permissions.
        manifest = patch_exactly_once(
            manifest,
            ATE_API_ROLE_ANCHOR,
            ATE_API_ROLE_WITH_DISCOVERY,
            "ate-api discovery role anchor",
        )
        # atelet blocks its gRPC listener until both discovery informers have
        # synchronized. The 0.0.11 chart only grants Pod reads, which leaves
        # every actor stuck in RESUMING while ateapi receives connection
        # refused from the node-local atelet endpoint.
        manifest = patch_exactly_once(
            manifest,
            ATELET_ROLE_ANCHOR,
            ATELET_ROLE_WITH_DISCOVERY,
            "atelet discovery role anchor",
        )
        # The 0.0.11 atelet image includes the credential broker and dials
        # ateapi, but the same-version chart neither mounts the servicedns CA
        # nor grants endpoint-slice discovery. Use cluster DNS for HA and mount
        # only the public serving trust bundle required by that client.
        manifest = patch_exactly_once(
            manifest,
            ATELET_CLIENT_ARGS_ANCHOR,
            ATELET_CLIENT_ARGS_WITH_DNS,
            "atelet ateapi client arguments",
        )
        manifest = patch_exactly_once(
            manifest,
            ATELET_VOLUMES_ANCHOR,
            ATELET_VOLUMES_WITH_SERVICEDNS,
            "atelet servicedns trust projection",
        )
        # The 0.0.11 chart emits the newer gateways/routes shape, while its
        # bundled agentgateway binary still consumes the established binds
        # schema. Keep the replacement exact and fail closed on chart drift.
        manifest = patch_exactly_once(
            manifest,
            AGENTGATEWAY_GATEWAYS_CONFIG,
            AGENTGATEWAY_BINDS_CONFIG,
            "agentgateway config schema",
        )
    else:
        raise SystemExit(f"unsupported Substrate GKE post-render profile: {profile}")

    sys.stdout.write(manifest)


if __name__ == "__main__":
    main()
