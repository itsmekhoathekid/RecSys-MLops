# RecSys Observability Evidence Guide

This guide captures the dashboard evidence for the observability rubric.

## Install

```bash
make observability-install
make mlops-install-serving
make data-platform-e2e
```

Open Grafana:

```bash
kubectl port-forward -n observability svc/recsys-grafana 3000:3000
```

Log in with `admin` / `admin`.

## Generate Demo Traffic

Forward the API first:

```bash
kubectl port-forward -n api-serving svc/recsys-api-serving 8088:80
```

Then generate traffic:

```bash
make observability-demo-traffic
```

## Required Screenshots

Save screenshots under `docs/observability/screenshots/`.

1. `web-api-overview.png`
   - Dashboard: `Web API Overview`
   - Must show requests/sec, total requests, failures, API latency, Redis/Triton breakdown.

2. `compute-telemetry.png`
   - Dashboard: `Compute Telemetry`
   - Must show CPU, RAM, and restart telemetry for `api-serving`, `recsys-dataflow`, `kserve-triton-inference`, `experiment-tracking`, or `kubeflow`.

3. `logs-overview.png`
   - Dashboard: `Logs Overview`
   - Must show Loki logs from API or Airflow/data-platform pods.

4. `traces-overview.png`
   - Dashboard: `Traces Overview`
   - Must show Tempo traces for `/recommendations`.

5. `ml-drift-retrain.png`
   - Dashboard: `ML Drift & Retrain`
   - Must show feature PSI, pass/fail, current/reference rows, and retrain trigger status.

## Rubric Mapping

| Rubric item | Dashboard |
| --- | --- |
| Web API metrics | `Web API Overview` |
| CPU/RAM/disk/network telemetry | `Compute Telemetry` |
| Logs | `Logs Overview` |
| Traces | `Traces Overview` |
| ML drift telemetry | `ML Drift & Retrain` |
| Retrain trigger | `ML Drift & Retrain` and `Logs Overview` |

No groundtruth labels are available in this project, so the ML dashboard reports feature drift, not model performance drift.
