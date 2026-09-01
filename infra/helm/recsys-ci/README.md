# RecSys CI

This chart installs the production Jenkins controller. The pipeline resolves
the only allowed Artifact Registry from `jenkins/config/gcp-production.json`;
the Helm chart does not expose registry job parameters.

Jenkins is kept out of Istio/service mesh by default by annotating the Jenkins
pod template with `sidecar.istio.io/inject: "false"`.

The chart seeds Jenkins jobs and views at startup:

- `00 Main Auto Deploy`: contains `RecSys-GitHub-CICD`, the GitHub webhook job.
  GitHub push deliveries call `/github-webhook/`, and the production job pins
  SCM checkout to `*/main` so each automatic build uses the exact main commit
  that publishes immutable images, deploys, and verifies production.
- `01 RAG Data Pipeline`: contains `RecSys-RAG-Data-Pipeline-CICD`.
- `02 Context Agent`: contains `RecSys-Context-Agent-CICD`.
- `03 Recommendation Agent`: contains `RecSys-Recommendation-Agent-CICD`.
- `04 Coordinator Agent`: contains `RecSys-Coordinator-Agent-CICD` and forces
  the complete specialist/MCP dependency closure.
- `06A KServe Model CD`: loads `jenkins/KServeModelCD.Jenkinsfile` from SCM on
  every shadow, A/B, evaluate, promote, or rollback build.

The four dedicated jobs use the root `Jenkinsfile`, set an explicit
`FORCE_COMPONENTS` scope, and default `FORCE_DEPLOY=true`. They are manual proof
jobs and therefore do not register duplicate GitHub webhook triggers.
The RAG proof job validates, builds, and deploys the RAG application and
infrastructure units. RAG data generation, embedding, and index promotion are
owned by the explicit Airflow/manual data workflow and are never started by
the root Jenkins release pipeline.

Production builds fail fast if publishing is disabled or the resolved registry
does not match the configured GCP Artifact Registry.

Install:

```bash
helm upgrade --install recsys-ci infra/helm/recsys-ci \
  --namespace ci \
  --create-namespace \
  --wait
```
