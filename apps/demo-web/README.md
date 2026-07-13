# Recommendation Demo Web

Production web application for exercising the RecSys realtime path:

`React -> FastAPI -> Postgres -> Debezium -> Kafka -> Flink -> Redis/Feast -> inference API`

The browser and API share one origin. The browser only calls relative `/api`
routes and never receives the gateway Basic Auth credential.

## Components

- `frontend/`: React, strict TypeScript, Vite, TanStack Query, generated OpenAPI types, and non-root NGINX image.
- `backend/`: FastAPI, bounded Psycopg pool, transactional event/order writes, HTTPX dependency clients, Prometheus, and OTLP tracing.
- `../../infra/helm/recsys-demo-web/`: atomic production release for both workloads, services, PDBs, ExternalSecret, GKE PodMonitoring (or optional ServiceMonitor), and apex ingress.

## Local verification

```bash
UV_CACHE_DIR=/tmp/recsys-demo-uv-cache bash jenkins/scripts/component_ci.sh demo_web
```

The component gate runs Ruff, pip-audit, pytest with a 90% backend coverage
minimum, npm audit, ESLint, Prettier, TypeScript, Vitest coverage, the production
frontend build, Helm lint, and Helm rendering.

## Production delivery

Changes under this app, its Helm chart, security contract, or demo tests select
the `demo_web` component. A `main` build creates `recsys-demo-api` and
`recsys-demo-web` images with the full Git SHA, runs Trivy for high/critical
findings, pushes both to Artifact Registry, records tag and digest manifests,
and performs an atomic Helm deployment.

On GKE the root Ingress sets `nginx.ingress.kubernetes.io/service-upstream=true`.
The ingress-nginx pod therefore targets the Service ClusterIP, allowing its
Istio sidecar to identify the destination and originate STRICT mTLS instead of
opening an unclassified passthrough connection directly to pod IPs.

Jenkins view `10 Recommendation Web App` contains:

- `RecSys-GitHub-CICD`: webhook/main pipeline.
- `RecSys-Recommendation-Web-CICD`: manual component rebuild/redeploy.
- `RecSys-Recommendation-Web-Rollback`: revision-selectable rollback plus smoke.

For authenticated public smoke, create a Jenkins username/password credential
named `recsys-demo-gateway-smoke`. Its value is injected only for the smoke
step. A missing credential skips the authenticated HTML assertion while all
internal, redirect, unauthenticated `401`, event-to-Feast, and recommendation
checks still run.

Manual rollback:

```bash
TARGET_REVISION=3 bash jenkins/scripts/demo_web_rollback.sh
```

Without `TARGET_REVISION`, the script selects the previous Helm revision.

## Production record

After a rollout, archive `.ci-image-manifest/demo_web.env`, `.demo-web/`, and the
Jenkins build artifacts. Record the Git SHA, Helm revision, and Jenkins build
URL in the deployment ticket; none of these mutable production values belong in
the chart defaults.

Current production deployment (2026-07-13):

- Application Git/image SHA: `a468c7b1bc4a118bf71415551566c04506b26b39`.
- Helm release: `recsys-demo-web`, namespace `api-serving`, revision `2`.
- Jenkins: `RecSys-GitHub-CICD` build `#61` ([cluster-internal build URL](http://recsys-jenkins.ci.svc.cluster.local:8080/job/RecSys-GitHub-CICD/61/)).
- Images: API dependency `sha256:da002d7ceafff04ab85f5dfe8fab777a7bb184826f7010170125620cafa2062c`, demo API `sha256:2f3c1e0ff7b34956a7d855ad9b4c1b259b154852506dcb7d5fb57ee369226cc6`, web `sha256:f5cb79ce89194c04b9e830860e499c4cf642a2768c06630448714fb3764e246f`.
