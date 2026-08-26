#!/usr/bin/env bash
set -Eeuo pipefail

# Substrate 0.0.11 unconditionally projects PodCertificate and
# ClusterTrustBundle sources into every WorkerPool pod. GKE can accept the Pod
# object while pruning those disabled beta fields, so checking api-resources is
# insufficient. A server-side dry run verifies that both projections survive
# admission before a maintenance upgrade changes the runtime.
rendered="$({
  cat <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  generateName: substrate-podcertificate-preflight-
  namespace: default
spec:
  restartPolicy: Never
  containers:
    - name: check
      image: registry.k8s.io/pause:3.10
      volumeMounts:
        - name: identity
          mountPath: /run/identity
          readOnly: true
  volumes:
    - name: identity
      projected:
        sources:
          - podCertificate:
              signerName: podidentity.podcert.ate.dev/identity
              keyType: ECDSAP256
              credentialBundlePath: credential-bundle.pem
          - clusterTrustBundle:
              signerName: podidentity.podcert.ate.dev/identity
              path: trust-bundle.pem
YAML
} | kubectl create --dry-run=server -o json -f -)"

python3 -c '
import json
import sys

pod = json.load(sys.stdin)
sources = pod["spec"]["volumes"][0]["projected"]["sources"]
if len(sources) != 2 or "podCertificate" not in sources[0] or "clusterTrustBundle" not in sources[1]:
    raise SystemExit(
        "GKE pruned PodCertificate/ClusterTrustBundle projections; "
        "Substrate 0.0.11 WorkerPool pods are incompatible with this cluster"
    )
' <<<"${rendered}"

kubectl api-resources --api-group=certificates.k8s.io \
  | grep -q 'podcertificaterequests' || {
    echo "PodCertificateRequest API is not served by this cluster" >&2
    exit 1
  }

echo "Substrate 0.0.11 PodCertificate and ClusterTrustBundle prerequisites passed."
