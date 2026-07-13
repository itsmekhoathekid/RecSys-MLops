# RecSys CI

This chart installs a Jenkins controller and a Docker Registry backed by a
cluster PVC.

Default image registry addresses:

- Jenkins push: `recsys-registry.ci.svc.cluster.local:5000/recsys`
- Workload pull: `localhost:5001/recsys`

The pull address uses a registry node proxy DaemonSet so kubelet can pull images
from the PVC-backed registry without relying on Kubernetes service DNS during
image pull.

Jenkins is kept out of Istio/service mesh by default by annotating the Jenkins
pod template with `sidecar.istio.io/inject: "false"`.

The chart seeds Jenkins jobs and views at startup:

- `00 Main Auto Deploy`: contains `RecSys-GitHub-CICD`, the GitHub webhook job.
  Push/merge events call `/github-webhook/`, Jenkins detects changed paths, runs
  test/build for affected components, and deploys changed components on `main`.
- `01 Materialize Pipeline` through `09 Streaming Online Store`: one manual
  proof job per coursework CI/CD pipeline. Each job uses the same `Jenkinsfile`
  with `FORCE_COMPONENTS=<component>` so its Stage View is easy to capture.
- `06B Progressive Model Rollout`: runs the shared main flow with
  `FORCE_COMPONENTS=rollout`, covering controller/model-CD tests, watcher image
  publishing, and watcher Deployment update.
- `06A KServe Model CD`: loads `jenkins/KServeModelCD.Jenkinsfile` from SCM on
  every shadow, A/B, evaluate, promote, or rollback build.
- `99 All Component CI/CD`: all manual component proof jobs in one overview.

On GKE, `values-gke.yaml` points Jenkins image push/pull parameters at
`asia-southeast1-docker.pkg.dev/fsds-coursework/recsys` and enables
`REQUIRE_GCP_ARTIFACT_REGISTRY`. Proof builds fail fast if image publishing is
disabled or the push registry is not GCP Artifact Registry.

Install:

```bash
helm upgrade --install recsys-ci infra/helm/recsys-ci \
  --namespace ci \
  --create-namespace \
  --wait
```
