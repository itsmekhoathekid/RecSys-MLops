#!/usr/bin/env bash
set -euo pipefail

GATEWAY_API_VERSION="${GATEWAY_API_VERSION:-v1.5.1}"
GAIE_VERSION="${GAIE_VERSION:-v1.5.0}"

GATEWAY_API_URL="https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/standard-install.yaml"
GAIE_URL="https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/${GAIE_VERSION}/v1-manifests.yaml"

echo "Installing Gateway API ${GATEWAY_API_VERSION} CRDs..."
kubectl apply --server-side -f "${GATEWAY_API_URL}"

echo "Installing Gateway API Inference Extension ${GAIE_VERSION} CRDs..."
kubectl apply --server-side -f "${GAIE_URL}"

kubectl wait --for=condition=Established \
  crd/gateways.gateway.networking.k8s.io \
  crd/httproutes.gateway.networking.k8s.io \
  crd/inferencepools.inference.networking.k8s.io \
  --timeout=180s
