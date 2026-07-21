# Routing & Gateway (NGINX Ingress Controller)

**Domain:** `recsys-mlops.site`

NGINX Ingress Controller is the public gateway for RecSys services. The backend
Kubernetes services stay internal, while DNS points the public subdomains to the
single NGINX LoadBalancer IP.

## Domain Setup For All 4 Services

| Service | Public domain | Internal backend |
| --- | --- | --- |
| Web API Pull Data service | `api.recsys-mlops.site` | `recsys-online-feature-api.api-serving.svc.cluster.local:80` |
| Metric service | `metrics.recsys-mlops.site` | `recsys-grafana.observability.svc.cluster.local:3000` |
| Log service | `log.recsys-mlops.site` | `recsys-loki.observability.svc.cluster.local:3100` |
| Trace service | `traces.recsys-mlops.site` | `recsys-tempo.observability.svc.cluster.local:3200` |

### Gateway Configuration Reference

| Gateway layer | Clickable configuration reference | Purpose |
| --- | --- | --- |
| NGINX Ingress Controller | [dependencies.tf (line 129)](../../../infra/terraform/gcp/dependencies.tf#L129) | Installs the cluster-wide `ingress-nginx` controller that receives public traffic. |
| Gateway Helm release | [recsys_services.tf (line 226)](../../../infra/terraform/gcp/recsys_services.tf#L226), [host/backend overrides (line 236)](../../../infra/terraform/gcp/recsys_services.tf#L236) | Deploys `recsys-gateway` and injects public hosts plus internal Kubernetes upstreams. |
| Shared gateway policy | [values.yaml: ingress class and domain](../../../infra/helm/recsys-gateway/values.yaml#L1), [Basic Auth](../../../infra/helm/recsys-gateway/values.yaml#L5), [TLS/cert-manager](../../../infra/helm/recsys-gateway/values.yaml#L16) | Centralizes the NGINX class, authentication secret, TLS issuer, certificates, and per-route rate limits. |
| Web API Pull Data route | [feature API values](../../../infra/helm/recsys-gateway/values.yaml#L46), [feature-api-ingress.yaml](../../../infra/helm/recsys-gateway/templates/feature-api-ingress.yaml#L1) | Routes the public API host to `recsys-online-feature-api` and applies Basic Auth, throttling, and TLS. |
| Metric route | [Grafana values](../../../infra/helm/recsys-gateway/values.yaml#L60), [grafana-ingress.yaml](../../../infra/helm/recsys-gateway/templates/grafana-ingress.yaml#L1) | Routes the metric domain to the internal Grafana service. |
| Log route | [Loki values](../../../infra/helm/recsys-gateway/values.yaml#L76), [logs-ingress.yaml](../../../infra/helm/recsys-gateway/templates/logs-ingress.yaml#L1), [root redirect](../../../infra/helm/recsys-gateway/templates/logs-root-redirect-ingress.yaml#L1) | Routes Loki API paths and optionally redirects the root path to the Grafana logs dashboard. |
| Trace route | [Tempo values](../../../infra/helm/recsys-gateway/values.yaml#L93), [traces-ingress.yaml](../../../infra/helm/recsys-gateway/templates/traces-ingress.yaml#L1) | Routes the trace domain to the internal Tempo service. |
| Gateway credentials | [auth-secrets.yaml](../../../infra/helm/recsys-gateway/templates/auth-secrets.yaml#L1) | Replicates the Basic Auth secret into namespaces that own the Ingress resources. |
| TLS issuer | [clusterissuer.yaml](../../../infra/helm/recsys-gateway/templates/clusterissuer.yaml#L1) | Optionally renders the cert-manager issuer used by HTTPS routes. |

The chart also contains a separate recommendation-serving route at [api-ingress.yaml](../../../infra/helm/recsys-gateway/templates/api-ingress.yaml#L1). It targets `recsys-api-serving`; the Web API Pull Data proof in this document targets `recsys-online-feature-api` through `feature-api-ingress.yaml`.

The public names shown here are deployment values. The checked-in chart defaults use `.recsys.local`, while Terraform derives hostnames from `gateway_domain`; production DNS names can therefore be supplied without changing the Ingress templates.

![Domain setup for gateway services](../../pngs/domain_setup.png)

**Figure: Domain setup for all gateway services.** The DNS provider has four
public `A` records: `api.recsys-mlops.site`, `metrics.recsys-mlops.site`,
`log.recsys-mlops.site`, and `traces.recsys-mlops.site`. All records point to
the NGINX Ingress Controller LoadBalancer IP `34.21.171.234`, proving that the
public domains enter the platform through the same gateway.


![Domain setup for gateway services](../../pngs/nginx_setup_4svcs.png)

**Figure: NGINX gateway, domain, and HTTPS setup for all 4 services.** The
proof shows the four public routes are configured on NGINX Ingress with their
production domains: `api.recsys-mlops.site`, `metrics.recsys-mlops.site`,
`log.recsys-mlops.site`, and `traces.recsys-mlops.site`. Each route is mapped
to its internal Kubernetes service and has HTTPS/TLS enabled, proving that the
gateway is the single secured entrypoint for the Web API, metrics, logs, and
traces services.

## Metric Service

The metric service is Grafana behind the NGINX gateway. The production host is
`https://metrics.recsys-mlops.site`.

### Code Reference

- [values.yaml (line 1)](../../../infra/helm/recsys-gateway/values.yaml#L1), [values.yaml (line 30)](../../../infra/helm/recsys-gateway/values.yaml#L30), [values.yaml (line 60)](../../../infra/helm/recsys-gateway/values.yaml#L60), [values.yaml (line 74)](../../../infra/helm/recsys-gateway/values.yaml#L74): gateway, TLS, authentication, Grafana host, and rate-limit values.
- [grafana-ingress.yaml (line 1)](../../../infra/helm/recsys-gateway/templates/grafana-ingress.yaml#L1), [grafana-ingress.yaml (line 47)](../../../infra/helm/recsys-gateway/templates/grafana-ingress.yaml#L47): renders the NGINX `Ingress` and security annotations.

### Basic Auth & Rate Limit Proof

![Basic auth challenge proof](../../pngs/metrics_auth_proof.png)

**Figure: Basic auth proof for metric service.** Accessing
`https://metrics.recsys-mlops.site` without valid gateway credentials returns a
Basic Auth challenge or `401 Unauthorized`, proving Grafana is protected before
the request reaches the internal `recsys-grafana` service.

![Gateway rate limit proof](../../pngs/metric_rate_limit.png)

**Figure: Rate limit proof for metric service.** The CLI proof shows the
Grafana ingress annotations and/or burst-test result for
`https://metrics.recsys-mlops.site`; excess requests are throttled by NGINX and
return HTTP `429`.

### Image Proof Enable HTTPS

![Metric service HTTPS proof](../../pngs/metric_https_proof.png)

**Figure: Metric service HTTPS proof.** The browser loads Grafana through
`https://metrics.recsys-mlops.site` with HTTPS enabled, proving the metric UI is
published through the NGINX gateway domain while the Kubernetes service remains
internal.

## Trace Service

The trace service is Tempo behind the NGINX gateway. The production host is
`https://traces.recsys-mlops.site`.

### Code Reference

- [values.yaml (line 1)](../../../infra/helm/recsys-gateway/values.yaml#L1), [values.yaml (line 30)](../../../infra/helm/recsys-gateway/values.yaml#L30), [values.yaml (line 93)](../../../infra/helm/recsys-gateway/values.yaml#L93), [values.yaml (line 105)](../../../infra/helm/recsys-gateway/values.yaml#L105): TLS/authentication plus Tempo host and rate-limit values.
- [traces-ingress.yaml (line 1)](../../../infra/helm/recsys-gateway/templates/traces-ingress.yaml#L1), [traces-ingress.yaml (line 45)](../../../infra/helm/recsys-gateway/templates/traces-ingress.yaml#L45): renders the trace route and security annotations.

### Basic Auth & Rate Limit Proof

![Basic auth challenge proof](../../pngs/traces_auth_proof.png)

**Figure: Basic auth proof for trace service.** Accessing
`https://traces.recsys-mlops.site` without valid gateway credentials returns a
Basic Auth challenge or `401 Unauthorized`, proving Tempo is protected at the
gateway layer.

![Gateway rate limit proof](../../pngs/traces_rate_limit.png)

**Figure: Rate limit proof for trace service.** The CLI proof shows the trace
ingress rate-limit annotations and/or burst-test result for
`https://traces.recsys-mlops.site`; NGINX returns HTTP `429` when requests exceed
the configured gateway limit.

### Image Proof Enable HTTPS

![Trace service HTTPS proof](../../pngs/traces_https_proof.png)

**Figure: Trace service HTTPS proof.** The trace endpoint is reached through
`https://traces.recsys-mlops.site`, proving HTTPS is enabled on the public trace
gateway route.

## Log Service

The log service is Loki behind the NGINX gateway. The production host is
`https://log.recsys-mlops.site`.

### Code Reference

- [values.yaml (line 1)](../../../infra/helm/recsys-gateway/values.yaml#L1), [values.yaml (line 30)](../../../infra/helm/recsys-gateway/values.yaml#L30), [values.yaml (line 76)](../../../infra/helm/recsys-gateway/values.yaml#L76), [values.yaml (line 91)](../../../infra/helm/recsys-gateway/values.yaml#L91): TLS/authentication plus Loki host, redirect, and rate-limit values.
- [logs-ingress.yaml (line 1)](../../../infra/helm/recsys-gateway/templates/logs-ingress.yaml#L1), [logs-ingress.yaml (line 70)](../../../infra/helm/recsys-gateway/templates/logs-ingress.yaml#L70): renders log routes, redirect, and security annotations.

### Basic Auth & Rate Limit Proof

![Basic auth challenge proof](../../pngs/logs_auth_proof.png)

**Figure: Basic auth proof for log service.** Accessing
`https://log.recsys-mlops.site` without valid gateway credentials returns a
Basic Auth challenge or `401 Unauthorized`, proving Loki is not publicly exposed
without gateway authentication.

![Gateway rate limit proof](../../pngs/logs_rate_limit.png)

**Figure: Rate limit proof for log service.** The CLI proof shows the Loki
ingress rate-limit annotations and/or burst-test result for
`https://log.recsys-mlops.site`; excess requests are throttled by NGINX with
HTTP `429`.

### Image Proof Enable HTTPS

![Log service HTTPS proof](../../pngs/logs_https_proof.png)

**Figure: Log service HTTPS proof.** The log endpoint is reached through
`https://log.recsys-mlops.site`, proving HTTPS is enabled on the public log
gateway route.


## Web API Pull Data Service

The Web API Pull Data service is the FastAPI online feature API behind the NGINX
gateway. The production host is `https://api.recsys-mlops.site`.

### Code Reference

- [feature_api.py (line 13)](../../../apps/api-serving/src/feature_api.py#L13), [feature_api.py (line 77)](../../../apps/api-serving/src/feature_api.py#L77): `RecSys Online Feature API` and POST/GET online-feature routes.
- [feature-api-ingress.yaml (line 1)](../../../infra/helm/recsys-gateway/templates/feature-api-ingress.yaml#L1), [feature-api-ingress.yaml (line 45)](../../../infra/helm/recsys-gateway/templates/feature-api-ingress.yaml#L45): route, Basic Auth, rate limit, and TLS annotations.
- [values.yaml (line 46)](../../../infra/helm/recsys-gateway/values.yaml#L46), [values.yaml (line 58)](../../../infra/helm/recsys-gateway/values.yaml#L58), [recsys_services.tf (line 226)](../../../infra/terraform/gcp/recsys_services.tf#L226), [recsys_services.tf (line 299)](../../../infra/terraform/gcp/recsys_services.tf#L299): enable the route and derive its host from `gateway_domain`.

### Basic Auth & Rate Limit Proof

![Basic auth challenge proof](../../pngs/pull_api_auth_proof.png)

**Figure: Basic auth proof for Web API Pull Data service.** Accessing
`https://api.recsys-mlops.site` without valid gateway credentials returns a
Basic Auth challenge or `401 Unauthorized`; authenticated traffic passes through
the gateway and reaches the FastAPI online-feature backend.

![Gateway rate limit proof](../../pngs/pull_api_rate_limit.png)

**Figure: Rate limit proof for Web API Pull Data service.** The CLI proof shows
the API ingress rate-limit annotations and/or burst-test result for
`https://api.recsys-mlops.site`; burst requests beyond the configured limit
return HTTP `429`.

### Image Proof Enable HTTPS

![Web API Pull Data HTTPS proof](../../pngs/pull_api_https_proof.png)

**Figure: Web API Pull Data HTTPS proof.** The FastAPI Swagger UI is loaded via
`https://api.recsys-mlops.site/docs`.
