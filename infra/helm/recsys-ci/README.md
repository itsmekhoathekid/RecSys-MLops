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

Install:

```bash
helm upgrade --install recsys-ci infra/helm/recsys-ci \
  --namespace ci \
  --create-namespace \
  --wait
```
