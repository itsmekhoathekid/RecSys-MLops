# kagent production HA proof -- 2026-08-28

This directory records the production GKE evidence for the kagent controller
HA criteria documented in [`../../benchmark_ha.md`](../../benchmark_ha.md).

## Deployment

- Release: `kagent`, namespace `kagent`.
- Chart: `kagent-0.10.0-e6df917`.
- Successful Helm revision: 24 at 2026-08-28 19:38:18 ICT.
- Controller Deployment after rollout: desired 3, updated 3, Ready 3,
  available 3.
- All controller Pods selected the `recsys-mlops-cpu` node pool and were spread
  2/1 across its two nodes.
- PDB `kagent-controller`: `maxUnavailable: 1`, healthy 3, desired healthy 2,
  disruptions allowed 1.
- Bundled PostgreSQL: Ready 1, available 1, image
  `docker.io/library/postgres:18.3-alpine`.

Revision 22 failed because a hard hostname topology constraint included the
tainted single ML-system node in its eligible-domain calculation. Helm rolled
it back automatically as revision 23. Pinning the controller to the CPU node
pool removed that ineligible domain; revision 24 then completed. This failed
attempt did not reduce controller availability below the pre-upgrade replica.

## Leader election and failover test

1. HTTP `GET /health` through `service/kagent-controller` returned
   `{"error":false,"data":{"status":"OK"},"message":"OK"}`.
2. Lease `0e9f6799.kagent.dev` had a 15-second duration and holder
   `kagent-controller-69c748cc6f-gnh6z_4e95f47a-83c2-417f-933b-9c2433c7d49b`.
3. The holder Pod `kagent-controller-69c748cc6f-gnh6z` was deleted.
4. Kubernetes recorded its container `Killing` event at
   `2026-08-28T12:41:11Z`.
5. Replica `kagent-controller-69c748cc6f-jjtjg` acquired the Lease at
   `2026-08-28T12:41:59.443922Z`; the observed event-to-acquire interval was
   approximately 48.4 seconds.
6. The Deployment recreated the missing Pod and returned to 3 desired, 3
   updated, 3 Ready, and 3 available replicas.
7. The same service health request again returned `status: OK`.

The health endpoint was sampled before and after failover, not continuously,
so this evidence proves recovery and successful takeover but does not establish
zero request interruption or an availability percentage.

## Scope limitation

The bundled PostgreSQL meets kagent's controller-replication prerequisite, but
it remains one database Pod and therefore a single point of failure. End-to-end
production HA requires an external highly available PostgreSQL service.

The exact captured values are retained in [`live-state.yaml`](live-state.yaml).
