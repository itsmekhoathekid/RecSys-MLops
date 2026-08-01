# RecSys CI

This chart installs the production Jenkins controller. The pipeline resolves
the only allowed Artifact Registry from `jenkins/config/gcp-production.json`;
the Helm chart does not expose registry job parameters.

Jenkins is kept out of Istio/service mesh by default by annotating the Jenkins
pod template with `sidecar.istio.io/inject: "false"`.

The chart seeds Jenkins jobs and views at startup:

- `00 Main Auto Deploy`: contains `RecSys-GitHub-CICD`, the GitHub webhook job.
  Push/merge events call `/github-webhook/`, Jenkins detects changed paths, runs
  test/build for affected components, and deploys changed components on `main`.
- `06A KServe Model CD`: loads `jenkins/KServeModelCD.Jenkinsfile` from SCM on
  every shadow, A/B, evaluate, promote, or rollback build.

Production builds fail fast if publishing is disabled or the resolved registry
does not match the configured GCP Artifact Registry.

Install:

```bash
helm upgrade --install recsys-ci infra/helm/recsys-ci \
  --namespace ci \
  --create-namespace \
  --wait
```
