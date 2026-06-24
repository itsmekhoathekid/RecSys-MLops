# RecSys NGINX Gateway

This gateway exposes the RecSys API and observability UIs through `ingress-nginx`.
All backend Kubernetes Services remain `ClusterIP`; external access goes through
Ingress only.

## Default Routes

| Host | Backend | Purpose |
| --- | --- | --- |
| `api.recsys.local` | `api-serving/recsys-api-serving:80` | Web API |
| `grafana.recsys.local` | `observability/recsys-grafana:3000` | Metrics/logs/traces UI |
| `logs.recsys.local` | `observability/recsys-loki:3100` | Direct Loki API/demo |
| `traces.recsys.local` | `observability/recsys-tempo:3200` | Direct Tempo API/demo |

Grafana is the preferred UI for metrics, logs, and traces. Loki and Tempo direct
Ingresses are mainly for rubric evidence and API checks.

## Local Minikube Flow

1. Install the RecSys services first:

   ```bash
   make observability-install
   make mlops-install-serving
   ```

2. Install `ingress-nginx`:

   ```bash
   make gateway-install-controller
   ```

3. Get the ingress address.

   For minikube, either run:

   ```bash
   minikube -p recsys-mlops tunnel
   ```

   Or use the minikube IP:

   ```bash
   minikube -p recsys-mlops ip
   ```

4. Add host records, replacing `MINIKUBE_IP`:

   ```text
   MINIKUBE_IP api.recsys.local grafana.recsys.local logs.recsys.local traces.recsys.local
   ```

5. Create Basic Auth credentials. Defaults are `recsys:recsys`, but it is better
   to generate a local htpasswd file:

   ```bash
   make gateway-create-auth USER=recsys PASSWORD='change-me'
   ```

6. Install the gateway:

   ```bash
   make gateway-install GATEWAY_DOMAIN=recsys.local
   ```

7. Smoke test:

   ```bash
   make gateway-smoke GATEWAY_DOMAIN=recsys.local GATEWAY_USER=recsys GATEWAY_PASSWORD='change-me'
   ```

For local clusters without public DNS, disable cert-manager TLS and use HTTP:

```bash
helm upgrade --install recsys-gateway infra/helm/recsys-gateway \
  --namespace api-serving \
  --set tls.enabled=false \
  --set gateway.domain=recsys.local

make gateway-smoke GATEWAY_SCHEME=http GATEWAY_CURL_FLAGS=
```

## Cloud HTTPS Flow

1. Install cert-manager.

   ```bash
   kubectl create namespace cert-manager --dry-run=client -o yaml | kubectl apply -f -
   helm repo add jetstack https://charts.jetstack.io
   helm repo update jetstack
   helm upgrade --install cert-manager jetstack/cert-manager \
     --namespace cert-manager \
     --set crds.enabled=true
   ```

2. Install `ingress-nginx` and get its external IP:

   ```bash
   make gateway-install-controller
   kubectl get svc -n ingress-nginx ingress-nginx-controller
   ```

3. Create DNS A records pointing to the ingress IP:

   ```text
   api.<domain>
   grafana.<domain>
   logs.<domain>
   traces.<domain>
   ```

4. Install with a Let's Encrypt staging issuer:

   ```bash
   make gateway-create-auth USER=recsys PASSWORD='change-me'

   helm upgrade --install recsys-gateway infra/helm/recsys-gateway \
     --namespace api-serving \
     --set gateway.domain=<domain> \
     --set api.host=api.<domain> \
     --set grafana.host=grafana.<domain> \
     --set logs.host=logs.<domain> \
     --set traces.host=traces.<domain> \
     --set tls.issuer.create=true \
     --set tls.issuer.email=<email> \
     --set-file auth.htpasswd=.gateway-auth/auth
   ```

5. Check certificate readiness:

   ```bash
   kubectl get certificate -A
   kubectl describe certificate -n api-serving recsys-api-tls
   ```

6. After staging passes, switch to production:

   ```bash
helm upgrade --install recsys-gateway infra/helm/recsys-gateway \
  --namespace api-serving \
  --set gateway.domain=<domain> \
  --set api.host=api.<domain> \
  --set grafana.host=grafana.<domain> \
  --set logs.host=logs.<domain> \
  --set traces.host=traces.<domain> \
  --set tls.clusterIssuerName=letsencrypt-prod \
  --set tls.issuer.create=true \
  --set tls.issuer.name=letsencrypt-prod \
     --set tls.issuer.server=https://acme-v02.api.letsencrypt.org/directory \
     --set tls.issuer.email=<email> \
     --set-file auth.htpasswd=.gateway-auth/auth
   ```

## Verification Checklist

Capture these as evidence:

- `kubectl get ingress -A` showing API, Grafana, Loki, and Tempo Ingresses.
- `curl -I https://api.<domain>/healthz` returning `401`.
- `curl -u user:pass https://api.<domain>/healthz` returning `200`.
- `curl -u user:pass -X POST https://api.<domain>/recommendations ...` returning `200`.
- A burst request showing `429` or the printed rate-limit distribution from `make gateway-smoke`.
- Browser screenshot of `https://grafana.<domain>` after Basic Auth.
- Grafana screenshots showing metrics, logs, and traces dashboards.
- `kubectl get certificate -A` showing API TLS certificate `READY=True` when using cert-manager.

## Security Notes

- Replace the default demo htpasswd before sharing a cluster.
- Keep `/metrics` behind Basic Auth externally; Prometheus scrapes the internal service directly.
- Do not expose Prometheus directly by default. Use Grafana for metric access.
- Rate limits are applied per `ingress-nginx` controller replica, so multiple replicas multiply the effective limit.
