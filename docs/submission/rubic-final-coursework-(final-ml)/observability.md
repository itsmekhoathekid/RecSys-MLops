# Observability Proof

This proof covers the final-coursework **Observability** requirement on GCP/GKE project `fsds-coursework`.

## Access

Grafana is opened through the GCP LoadBalancer gateway, not a local Grafana instance.

```bash
GATEWAY_IP="$(kubectl -n ingress-nginx get svc ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"

sudo sh -c "printf '%s %s\n' '${GATEWAY_IP}' 'grafana.recsys.local logs.recsys.local traces.recsys.local api.recsys.local' >> /etc/hosts"
```

```text
Grafana URL:        http://grafana.recsys.local
Gateway basic auth: loaded from ignored `.env` (`GATEWAY_USER` / `GATEWAY_PASSWORD`)
Grafana login:      admin / admin
Grafana folder:     RecSys
```

Dashboard links:

```text
http://grafana.recsys.local/d/recsys-web-api/web-api-overview
http://grafana.recsys.local/d/recsys-compute/compute-telemetry
http://grafana.recsys.local/d/recsys-logs/logs-overview
http://grafana.recsys.local/d/recsys-traces/traces-overview
http://grafana.recsys.local/d/recsys-ml-drift/ml-drift-retrain
```

Gateway proof:

![Grafana gateway](../../pngs/grafana_gateway.png)

## Runtime Stack

The observability stack is deployed in namespace `observability`.

```bash
kubectl -n observability get svc recsys-grafana recsys-prometheus recsys-loki recsys-tempo recsys-pushgateway -o wide
kubectl -n observability get pods -o wide
kubectl -n observability get configmap -l grafana_dashboard=1
```

Observed services:

```text
recsys-grafana       ClusterIP   3000/TCP
recsys-prometheus    ClusterIP   9090/TCP
recsys-loki          ClusterIP   3100/TCP
recsys-tempo         ClusterIP   3200/TCP,4317/TCP,4318/TCP
recsys-pushgateway   ClusterIP   9091/TCP
```

Observed pods:

```text
recsys-grafana        1/1 Running
recsys-prometheus     1/1 Running
recsys-loki           1/1 Running
recsys-tempo          1/1 Running
recsys-pushgateway    1/1 Running
recsys-promtail       2/2 Running on both CPU and ML node pools
redis-exporter        1/1 Running
postgres-exporters    1/1 Running
```

Stack screenshots:

![Observability services](../../pngs/observe_svcs.png)

![Observability pods](../../pngs/observe_pods.png)

![Grafana dashboard ConfigMaps](../../pngs/obser_config_map.png)

## Requirement Mapping

| Requirement | Implementation | Runtime proof | Screenshot |
|---|---|---|---|
| Web API metrics: request rate, total requests, failures, latency | FastAPI exposes `/metrics`; Prometheus scrapes `recsys-api-serving` | `POST /recommendations 200 = 140`, plus `/healthz`, `/ready`, `/metrics` counters | ![Web API overview](../../pngs/web_api_overview.png) |
| Compute telemetry: CPU, RAM, network, pod health | Prometheus scrapes kubelet/cAdvisor metrics | CPU/memory split is visible across `api-serving`, `recsys-dataflow`, `kubeflow`, `datahub`, `experiment-tracking`, and `kserve-triton-inference` | ![Compute telemetry](../../pngs/compute_telemetry.png) |
| Logs | Promtail ships pod logs to Loki | Loki query returned `498` API-serving log lines in 10 minutes and `120` `/recommendations` lines | ![Logs overview](../../pngs/logs_overview.png) |
| Traces | FastAPI OpenTelemetry exports traces to Tempo; JSON logs include trace IDs | Tempo search returns traces for `rootServiceName=recsys-api-serving` | ![Traces overview](../../pngs/traces_overview_.png) |
| ML drift telemetry | Airflow drift task pushes PSI metrics to PushGateway; Prometheus scrapes PushGateway | Dashboard shows PSI leaderboard, feature pass/fail, current/reference rows | ![ML drift Grafana](../../pngs/drift_ml_grafana.png) |
| Retrain trigger telemetry | Airflow retrain task pushes trigger/failure counters to PushGateway | `recsys_ml_retrain_triggered_total{reason="feature_drift"} = 1`; failure count is `0` | ![ML drift Grafana](../../pngs/drift_ml_grafana.png) |

Groundtruth note: this demo has no production labels, so drift is measured with PSI between current offline feature-store data and a reference baseline. The report marks `groundtruth_available=false`.

## Live Verification Commands

Generate request, metric, log, and trace data:

```bash
set -a
source .env
set +a

curl -s -u "${GATEWAY_USER}:${GATEWAY_PASSWORD}" \
  -H 'Host: api.recsys.local' \
  -H 'Content-Type: application/json' \
  -X POST "http://${GATEWAY_IP}/recommendations" \
  -d '{"user_id":1,"candidate_item_ids":[101,202,303],"top_k":3}'
```

Prometheus counter proof:

```bash
kubectl -n observability exec deploy/recsys-prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/query?query=sum%28recsys_api_requests_total%29%20by%20%28route%2Cmethod%2Cstatus%29'
```

Observed:

```text
GET  /healthz          200
GET  /ready            200
GET  /metrics          200
POST /recommendations  200
```

Loki log proof:

```bash
kubectl -n observability exec deploy/recsys-loki -- \
  wget -qO- 'http://localhost:3100/loki/api/v1/query?query=sum%28count_over_time%28%7Bnamespace%3D%22api-serving%22%7D%20%7C%3D%20%22recommendations%22%5B10m%5D%29%29'
```

Observed:

```text
/recommendations log lines in last 10m: 120
```

Tempo trace proof:

```bash
kubectl -n observability exec deploy/recsys-tempo -- \
  wget -qO- 'http://localhost:3200/api/search?tags=service.name%3Drecsys-api-serving&limit=5'
```

Observed:

```text
rootServiceName=recsys-api-serving
rootTraceName=GET /ready, GET /healthz, GET /metrics
```

## Screenshot Checklist

| Screenshot | Requirement covered |
|---|---|
| ![Observability services](../../pngs/observe_svcs.png) | Observability services are deployed |
| ![Observability pods](../../pngs/observe_pods.png) | Grafana, Prometheus, Loki, Tempo, PushGateway, Promtail are running |
| ![Grafana dashboard ConfigMaps](../../pngs/obser_config_map.png) | Grafana dashboards are provisioned by Kubernetes ConfigMaps |
| ![Grafana gateway](../../pngs/grafana_gateway.png) | Grafana is accessible through GCP gateway |
| ![Web API overview](../../pngs/web_api_overview.png) | Web API metrics |
| ![Compute telemetry](../../pngs/compute_telemetry.png) | Compute telemetry |
| ![Logs overview](../../pngs/logs_overview.png) | Centralized logs |
| ![Traces overview](../../pngs/traces_overview_.png) | Distributed traces |
| ![ML drift Grafana](../../pngs/drift_ml_grafana.png) | ML drift and retrain telemetry |

## Verification Summary

| Requirement | Status |
|---|---:|
| Web API metrics | PASS |
| Compute telemetry | PASS |
| Logs | PASS |
| Traces | PASS |
| ML drift telemetry | PASS |
| Retrain trigger telemetry | PASS |
