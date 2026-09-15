# A/B capacity attempt — 2026-09-09

Outcome: NOT ACCEPTED; all six applied CPU request changes restored.
No A/B inference, Q8 deployment, Jenkins activation, or data deletion occurred.

The user authorized the proposed temporary infrastructure capacity window.
Read-only inspection found no active Argo workflows, Ray clusters, or Kubeflow
scheduled workflows; the two historical Ray jobs were complete. Jenkins PVC
zone affinity is compatible with both existing nodes.

The batch scale-down command (Airflow, DataHub, MLflow and all Kubeflow
Deployments) was rejected by auto-review for broad production disruption.
It did not execute. No workaround scale-down was attempted.

Separately approved CPU-only changes retained replicas, memory and limits:

- Three agent WorkerPools: 250m to 100m each.
- Baseline EPP: 250m to 50m.
- ClickHouseCluster: 500m to 250m.
- Triton InferenceService model: 1 CPU to 300m.

Triton's replacement pod could not schedule while the old pod retained its
requests on E2. ClickHouse restarted during the resource rollout. With offline
capacity unavailable, all six changes were restored through the field journal.
Triton rollout and ClickHouse rollout subsequently completed; the three pools,
EPP, ClickHouse and Triton were checked again for readiness.

Journal: `evidence/ab-capacity-2026-09-09.json`. Recovery utility:
`ops/recovery/ab_capacity_window.py`; it records only touched fields and uses
resource-version checks, refusing conflicting changes during restore.

Before another attempt, explicitly approve a bounded list of offline services
and their downtime, check consumer dependencies for each database/operator,
and release capacity before starting resource rollouts. Existing capacity
arithmetic did not reserve enough transient E2 surge space. Do not call the
full workflow preflight PASS or claim production A/B readiness from this attempt.

## Explicitly approved bounded retry

The user subsequently approved temporary downtime for Airflow, DataHub and
MLflow tracking. Airflow's metadata query found no running/queued/restarting
tasks. Each of the three private database StatefulSets had Retain policies for
both scale-down and deletion. The live pod-spec reference scan and repository
references showed the stopped DataHub GMS as the remaining application consumer
of its private MySQL/OpenSearch endpoints; this scan does not prove absence of
external clients. The user-authorized service downtime covers those endpoints.

Applied in order, with field snapshots in the same journal:

1. Airflow scheduler/webserver, DataHub GMS, MLflow Deployment replicas to zero.
2. Airflow's private PostgreSQL replicas to zero.
3. Wait for GMS graceful termination (120-second grace; no force deletion).
4. DataHub private MySQL/OpenSearch replicas to zero.

Shared experiment-tracking PostgreSQL/MinIO, feature PostgreSQL, both serving
paths, telemetry, and all Kubeflow workloads were left unchanged. PVCs retained.

Measured free requests after shutdown:

| Node | CPU | RAM MiB | Hypothetical CPU after previously proposed rightsizing |
| --- | ---: | ---: | ---: |
| N2 / cpu-services | 0.193 | 2780.5 | 1.543 |
| E2 / ml-system | 0.541 | 6265.1 | 1.241 |

The read-only scheduling checker returned HOLD for a Q8 2 CPU / 3 GiB demand.
Even hypothetical rightsizing does not leave 2 CPU on N2. No rightsizing was
re-applied, no Q8 pod created, no Jenkins activated and no inference executed.
Per the user's original restore-if-insufficient requirement, all seven replica
changes were restored. The journal records RESTORED for these entries.

Recovery verification: Airflow scheduler/webserver/PostgreSQL, MLflow and
DataHub MySQL returned Ready. OpenSearch completed an image pull and normal
startup, then its Pod Ready condition passed. DataHub GMS subsequently completed
its Deployment rollout successfully. All 13 journal entries across both attempts
are RESTORED. Recommendation/RAG APIs and baseline Q4 remained Ready at the
post-restore check. No data-integrity equivalence beyond retained PVCs and
service readiness is asserted.
