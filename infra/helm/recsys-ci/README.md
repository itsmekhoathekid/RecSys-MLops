# RecSys CI

This chart installs the production Jenkins controller. Images are pushed to and
pulled from GCP Artifact Registry; the default values use `example.invalid` so a
plain render cannot accidentally target a real registry.

Jenkins is kept out of Istio/service mesh by default by annotating the Jenkins
pod template with `sidecar.istio.io/inject: "false"`.

The chart seeds Jenkins jobs and views at startup:

- `00 Main Auto Deploy`: contains `RecSys-GitHub-CICD`, the GitHub webhook job.
  Push/merge events call `/github-webhook/`, Jenkins detects changed paths, runs
  test/build for affected components, and deploys changed components on `main`.
- `06A KServe Model CD`: loads `jenkins/KServeModelCD.Jenkinsfile` from SCM on
  every shadow, A/B, evaluate, promote, or rollback build.

On GKE, `values-gke.yaml` points Jenkins image push/pull parameters at
`asia-southeast1-docker.pkg.dev/rec-sys-503309/recsys` and enables
`REQUIRE_GCP_ARTIFACT_REGISTRY`. Builds fail fast if image publishing is
disabled or the push registry is not GCP Artifact Registry.

Install:

```bash
helm upgrade --install recsys-ci infra/helm/recsys-ci \
  --namespace ci \
  --create-namespace \
  --wait
```
