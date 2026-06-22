# Kubeflow + MLflow + Ray Local Runbook

Runbook nay mo ta cach setup, run smoke E2E va monitor full flow local K8s cho:

- Kubeflow Pipelines lam workflow/control plane.
- KubeRay `RayJob` lam tuning + training.
- MLflow tracking server.
- MinIO lam MLflow artifact store/model weight storage.
- PostgreSQL lam MLflow backend store va `model_configs` registry.

Target hien tai:

- `minikube` profile: `recsys-mlops`
- K8s context: `recsys-mlops`
- Namespace KFP/KubeRay: `kubeflow`
- Namespace MLflow stack: `mlops`
- CPU local smoke tren macOS arm64.
- GPU overlay de danh cho Linux/NVIDIA local K8s sau nay.

## 1. Preflight

Kiem tra Docker Desktop dang chay va context dung:

```bash
docker info
kubectl config current-context
```

Context phai la:

```bash
recsys-mlops
```

Neu chua co Minikube profile:

```bash
minikube start \
  --profile recsys-mlops \
  --driver=docker \
  --cpus=6 \
  --memory=7168 \
  --disk-size=40g

kubectl config use-context recsys-mlops
```

Neu local dataflow compose dang an RAM, nen stop truoc khi chay Ray smoke:

```bash
docker compose -f infra/docker/docker-compose.dataflow.yml stop
```

## 2. Build Images Trong Minikube

Build image vao Docker daemon cua Minikube de pod dung `imagePullPolicy=Never`:

```bash
eval "$(minikube -p recsys-mlops docker-env)"
make mlops-images
```

Images can co:

```bash
docker images | grep recsys
```

Expected:

- `recsys-base-python:local`
- `recsys-mlops-training:local`
- `recsys-mlflow:local`

## 3. Deploy Kubeflow, KubeRay, MLflow Stack

Install Kubeflow Pipelines:

```bash
make mlops-install-kfp
```

Install KubeRay operator:

```bash
make mlops-install-kuberay
```

Install MLflow + MinIO + Postgres va runtime PVC/secret:

```bash
make mlops-install-stack
```

Check readiness:

```bash
kubectl get deploy -n kubeflow
kubectl get pods -n kubeflow
kubectl get pods -n mlops
```

Critical deployments nen Ready:

- `ml-pipeline`
- `ml-pipeline-ui`
- `workflow-controller`
- `metadata-grpc-deployment`
- `kuberay-operator`
- `mlflow`
- `minio`
- `postgres`

MacOS arm64 caveat:

- `metadata-writer` co the can scale `0` vi image arch.
- `proxy-agent` co the can scale `0` vi PodSecurity/hostNetwork.

```bash
kubectl scale deploy -n kubeflow metadata-writer proxy-agent --replicas=0
```

Neu can giam RAM de chay Ray smoke, co the tam scale down KFP core, sau smoke thi scale up lai:

```bash
kubectl scale deploy -n kubeflow \
  cache-server ml-pipeline ml-pipeline-persistenceagent mysql seaweedfs workflow-controller \
  --replicas=0
```

Scale up lai:

```bash
kubectl scale deploy -n kubeflow \
  cache-deployer-deployment cache-server controller-manager \
  metadata-envoy-deployment metadata-grpc-deployment \
  ml-pipeline ml-pipeline-persistenceagent ml-pipeline-scheduledworkflow \
  ml-pipeline-ui ml-pipeline-viewer-crd ml-pipeline-visualizationserver \
  mysql seaweedfs workflow-controller \
  --replicas=1
```

## 4. Compile KFP Pipeline

Neu local env co `kfp`:

```bash
RECSYS_PIPELINE_IMAGE=recsys-mlops-training:local \
uv run python apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py
```

Neu local env khong co `kfp`, compile bang training image:

```bash
docker run --rm \
  -v "$PWD:/opt/recsys" \
  -w /opt/recsys \
  -e RECSYS_PIPELINE_IMAGE=recsys-mlops-training:local \
  recsys-mlops-training:local \
  uv run python apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py
```

Output:

```bash
infra/kubeflow/compiled/bst_training_pipeline.yaml
```

KFP UI:

```bash
kubectl port-forward -n kubeflow svc/ml-pipeline-ui 8080:80
```

Mo:

```text
http://127.0.0.1:8080
```

Upload file pipeline YAML o tren vao Kubeflow Pipelines UI.

## 5. Prepare PVC Data Cho Local Smoke

Tao helper pod mount PVC:

```bash
kubectl run recsys-pvc-loader \
  -n kubeflow \
  --restart=Never \
  --image=recsys-mlops-training:local \
  --image-pull-policy=Never \
  --overrides='{"spec":{"containers":[{"name":"recsys-pvc-loader","image":"recsys-mlops-training:local","imagePullPolicy":"Never","command":["sleep","36000"],"volumeMounts":[{"name":"recsys-shared","mountPath":"/workspace"}]}],"volumes":[{"name":"recsys-shared","persistentVolumeClaim":{"claimName":"recsys-mlops-pvc"}}]}}'
```

Copy small dataset vao PVC:

```bash
kubectl exec -n kubeflow recsys-pvc-loader -- mkdir -p /workspace/recsys/apps/data-platform/data-generator/src/output
kubectl cp \
  apps/data-platform/data-generator/src/output/test_10k_seed42 \
  kubeflow/recsys-pvc-loader:/workspace/recsys/apps/data-platform/data-generator/src/output/test_10k_seed42
```

Verify:

```bash
kubectl exec -n kubeflow recsys-pvc-loader -- \
  ls /workspace/recsys/apps/data-platform/data-generator/src/output/test_10k_seed42
```

## 6. Run Direct Smoke E2E

Direct smoke dung cung image/PVC/secret voi KFP components. Cach nay huu ich de debug tung step.

### 6.1 Feature Engineering

```bash
kubectl run recsys-fe-smoke \
  -n kubeflow \
  --restart=Never \
  --image=recsys-mlops-training:local \
  --image-pull-policy=Never \
  --overrides='{"spec":{"containers":[{"name":"recsys-fe-smoke","image":"recsys-mlops-training:local","imagePullPolicy":"Never","envFrom":[{"secretRef":{"name":"recsys-mlops-runtime"}}],"command":["python","-m","recsys_model_pipeline.run_feature_engineering","--input-dir","/workspace/recsys/apps/data-platform/data-generator/src/output/test_10k_seed42","--output-dir","/workspace/recsys/data_platform/output","--summary-path","/workspace/recsys/data_platform/output/feature_summary.json"],"volumeMounts":[{"name":"recsys-shared","mountPath":"/workspace"}],"resources":{"requests":{"cpu":"100m","memory":"512Mi"},"limits":{"cpu":"1","memory":"2Gi"}}}],"volumes":[{"name":"recsys-shared","persistentVolumeClaim":{"claimName":"recsys-mlops-pvc"}}]}}'
```

Watch:

```bash
kubectl get pod -n kubeflow recsys-fe-smoke -w
kubectl logs -n kubeflow recsys-fe-smoke -f
```

Expected summary for 10k smoke:

- `ml_bst_training`: about `19266`
- `user_sequence_features`: about `9845`
- `item_features`: about `9845`

### 6.2 Prepare BST JSONL Splits

```bash
kubectl run recsys-prepare-smoke \
  -n kubeflow \
  --restart=Never \
  --image=recsys-mlops-training:local \
  --image-pull-policy=Never \
  --overrides='{"spec":{"containers":[{"name":"recsys-prepare-smoke","image":"recsys-mlops-training:local","imagePullPolicy":"Never","command":["python","-m","recsys_model_pipeline.prepare_bst_training_data","--input-path","/workspace/recsys/data_platform/output/ml/offline/ml_bst_training","--output-dir","/workspace/recsys/notebooks/data/bst_split","--metadata-path","/workspace/recsys/data_platform/output/ml/bst_split_meta.json"],"volumeMounts":[{"name":"recsys-shared","mountPath":"/workspace"}],"resources":{"requests":{"cpu":"100m","memory":"512Mi"},"limits":{"cpu":"1","memory":"2Gi"}}}],"volumes":[{"name":"recsys-shared","persistentVolumeClaim":{"claimName":"recsys-mlops-pvc"}}]}}'
```

Watch:

```bash
kubectl get pod -n kubeflow recsys-prepare-smoke -w
kubectl logs -n kubeflow recsys-prepare-smoke
```

Expected split for 10k smoke:

- train: `15412`
- val: `1926`
- test: `1928`

### 6.3 Submit RayJob Tune + Train

```bash
kubectl run recsys-submit-ray-smoke \
  -n kubeflow \
  --restart=Never \
  --image=recsys-mlops-training:local \
  --image-pull-policy=Never \
  --overrides='{"spec":{"serviceAccountName":"pipeline-runner","containers":[{"name":"recsys-submit-ray-smoke","image":"recsys-mlops-training:local","imagePullPolicy":"Never","envFrom":[{"secretRef":{"name":"recsys-mlops-runtime"}}],"command":["python","-m","recsys_model_pipeline.submit_ray_job","--namespace","kubeflow","--job-name","recsys-bst-ray-direct","--image","recsys-mlops-training:local","--base-config-path","/opt/recsys/configs/local/bst.yaml","--split-dir","/workspace/recsys/notebooks/data/bst_split","--ray-output-dir","/workspace/recsys/data_platform/output/ml/ray","--best-result-path","/workspace/recsys/data_platform/output/ml/ray/best_result.json","--training-percent","0.01","--num-epochs","1","--max-trials","2","--parallel-trials","1","--pvc-name","recsys-mlops-pvc","--runtime-secret-name","recsys-mlops-runtime","--head-cpu-request","500m","--head-cpu-limit","1","--head-memory-request","1Gi","--head-memory-limit","3Gi","--worker-cpu-request","500m","--worker-cpu-limit","2","--worker-memory-request","1Gi","--worker-memory-limit","2Gi","--worker-replicas","1","--timeout-seconds","1800"],"resources":{"requests":{"cpu":"100m","memory":"256Mi"},"limits":{"cpu":"500m","memory":"512Mi"}}}]}}'
```

Monitor:

```bash
kubectl get rayjob,raycluster -n kubeflow -w
kubectl get pods -n kubeflow | grep recsys-bst-ray-direct
kubectl logs -n kubeflow recsys-submit-ray-smoke -f
```

Lay driver pod:

```bash
kubectl get pods -n kubeflow | grep recsys-bst-ray-direct
```

Driver pod thuong co name dang:

```text
recsys-bst-ray-direct-xxxxx
```

Doc driver logs:

```bash
kubectl logs -n kubeflow <driver-pod-name> -f
```

Expected:

- `RayJob` chuyen `RUNNING`.
- 2 Ray Tune trials `TERMINATED`.
- `RayJob` chuyen `SUCCEEDED`.
- `best_result.json` duoc ghi vao PVC.

Check best result:

```bash
kubectl exec -n kubeflow recsys-pvc-loader -- \
  cat /workspace/recsys/data_platform/output/ml/ray/best_result.json
```

### 6.4 Evaluate Best Checkpoint

```bash
kubectl run recsys-eval-smoke \
  -n kubeflow \
  --restart=Never \
  --image=recsys-mlops-training:local \
  --image-pull-policy=Never \
  --overrides='{"spec":{"containers":[{"name":"recsys-eval-smoke","image":"recsys-mlops-training:local","imagePullPolicy":"Never","envFrom":[{"secretRef":{"name":"recsys-mlops-runtime"}}],"command":["python","-m","recsys_model_pipeline.evaluate_ray_best_bst","--ray-result-path","/workspace/recsys/data_platform/output/ml/ray/best_result.json","--split","test","--metrics-path","/workspace/recsys/data_platform/output/ml/eval_metrics.json"],"volumeMounts":[{"name":"recsys-shared","mountPath":"/workspace"}],"resources":{"requests":{"cpu":"100m","memory":"512Mi"},"limits":{"cpu":"1","memory":"2Gi"}}}],"volumes":[{"name":"recsys-shared","persistentVolumeClaim":{"claimName":"recsys-mlops-pvc"}}]}}'
```

Monitor:

```bash
kubectl get pod -n kubeflow recsys-eval-smoke -w
kubectl logs -n kubeflow recsys-eval-smoke -f
```

Read test metrics:

```bash
kubectl exec -n kubeflow recsys-pvc-loader -- \
  cat /workspace/recsys/data_platform/output/ml/eval_metrics.json
```

Smoke run da verify:

- `auc`: about `0.50016`
- `ndcg@10`: about `0.094917`
- `loss`: about `0.47398`
- `num_groups`: `1928`

## 7. Verify MLflow, MinIO, Postgres

Port-forward MLflow UI:

```bash
kubectl port-forward -n mlops svc/mlflow 5000:5000
```

Mo:

```text
http://127.0.0.1:5000
```

Check MLflow runs trong Postgres:

```bash
kubectl exec -n mlops deploy/postgres -- \
  env PGPASSWORD=mlflow123 \
  psql -U mlflow -d mlflow \
  -c "select run_uuid, name, artifact_uri, status from runs order by start_time desc limit 8;"
```

Check model registry table:

```bash
kubectl exec -n mlops deploy/postgres -- \
  env PGPASSWORD=mlflow123 \
  psql -U mlflow -d mlflow \
  -c "select model_name, model_version, artifact_uri, mlflow_run_id, created_at from model_configs order by created_at desc limit 5;"
```

Check MinIO artifact files:

```bash
kubectl exec -n mlops deploy/minio -- \
  sh -c "ls -R /data/mlflow-artifacts/1/<mlflow-run-id>/artifacts"
```

Expected artifact dirs:

- `model/BST/.../part.1`
- `configs/local/bst.yaml`
- `metrics/training_metrics.json`

## 8. Monitor Full Kubeflow Logs

### 8.1 Status Overview

```bash
kubectl get pods -A
kubectl get deploy -n kubeflow
kubectl get pods -n kubeflow
kubectl get pods -n mlops
kubectl get rayjob,raycluster -A
kubectl get events -n kubeflow --sort-by=.lastTimestamp
kubectl get events -n mlops --sort-by=.lastTimestamp
```

### 8.2 KFP API, UI, Workflow Logs

```bash
kubectl logs -n kubeflow deploy/ml-pipeline -f
kubectl logs -n kubeflow deploy/ml-pipeline-ui -f
kubectl logs -n kubeflow deploy/workflow-controller -f
kubectl logs -n kubeflow deploy/ml-pipeline-persistenceagent -f
kubectl logs -n kubeflow deploy/cache-server -f
```

Metadata:

```bash
kubectl logs -n kubeflow deploy/metadata-grpc-deployment -f
kubectl logs -n kubeflow deploy/metadata-envoy-deployment -f
```

MySQL/SeaweedFS:

```bash
kubectl logs -n kubeflow deploy/mysql -f
kubectl logs -n kubeflow deploy/seaweedfs -f
```

### 8.3 KFP Runs/Argo Workflows

List workflows:

```bash
kubectl get workflows -n kubeflow
```

Describe workflow:

```bash
kubectl describe workflow -n kubeflow <workflow-name>
```

Get pods of a workflow:

```bash
kubectl get pods -n kubeflow -l workflows.argoproj.io/workflow=<workflow-name>
```

Logs cua mot step pod:

```bash
kubectl logs -n kubeflow <step-pod-name> -c main -f
```

Neu pod co init container:

```bash
kubectl logs -n kubeflow <step-pod-name> -c init -f
```

### 8.4 KubeRay Operator, RayCluster, RayJob

KubeRay operator:

```bash
kubectl logs -n kubeflow deploy/kuberay-operator -f
```

Ray resources:

```bash
kubectl get rayjob,raycluster -n kubeflow
kubectl describe rayjob -n kubeflow recsys-bst-ray-direct
kubectl describe raycluster -n kubeflow <raycluster-name>
```

Ray pods:

```bash
kubectl get pods -n kubeflow | grep recsys-bst-ray-direct
```

Head log:

```bash
kubectl logs -n kubeflow <ray-head-pod> -c ray-head -f
kubectl logs -n kubeflow <ray-head-pod> -c ray-head --previous
```

Worker log:

```bash
kubectl logs -n kubeflow <ray-worker-pod> -c ray-worker -f
kubectl logs -n kubeflow <ray-worker-pod> -c ray-worker --previous
```

Driver log:

```bash
kubectl logs -n kubeflow <ray-driver-pod> -f
```

Ray dashboard port-forward:

```bash
kubectl get svc -n kubeflow | grep head-svc
kubectl port-forward -n kubeflow svc/<ray-head-service> 8265:8265
```

Mo:

```text
http://127.0.0.1:8265
```

### 8.5 MLflow, MinIO, Postgres Logs

```bash
kubectl logs -n mlops deploy/mlflow -f
kubectl logs -n mlops deploy/minio -f
kubectl logs -n mlops deploy/postgres -f
```

Port-forward:

```bash
kubectl port-forward -n mlops svc/mlflow 5000:5000
kubectl port-forward -n mlops svc/minio 9001:9001
```

MLflow:

```text
http://127.0.0.1:5000
```

MinIO console:

```text
http://127.0.0.1:9001
```

Default local credentials from Helm values:

- MinIO user: `minio`
- MinIO password: `minio123`
- Postgres user: `mlflow`
- Postgres password: `mlflow123`
- Postgres db: `mlflow`

## 9. Cleanup

Delete smoke pods:

```bash
kubectl delete pod -n kubeflow \
  recsys-fe-smoke \
  recsys-prepare-smoke \
  recsys-submit-ray-smoke \
  recsys-eval-smoke \
  --ignore-not-found
```

Delete RayJob after run:

```bash
kubectl delete rayjob -n kubeflow recsys-bst-ray-direct --ignore-not-found
```

Keep PVC helper if still copying/inspecting data. Delete it when done:

```bash
kubectl delete pod -n kubeflow recsys-pvc-loader --ignore-not-found
```

Stop Minikube:

```bash
minikube stop -p recsys-mlops
```

Delete Minikube profile only when you want to remove all local cluster state:

```bash
minikube delete -p recsys-mlops
```

## 10. GPU Overlay Later

GPU profile is prepared for Linux/NVIDIA local K8s. Before applying GPU values:

```bash
kubectl describe node | grep -A5 "Allocatable"
kubectl get nodes -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}'
```

Expected GPU allocatable must include `nvidia.com/gpu`.

Render GPU Ray chart:

```bash
helm template recsys-ray-gpu infra/helm/ray-cluster \
  --namespace kubeflow \
  -f infra/helm/ray-cluster/values-gpu.yaml
```

GPU worker should request/limit:

```yaml
nvidia.com/gpu: 1
```

And use:

```yaml
nodeSelector:
  nvidia.com/gpu.present: "true"
```

For macOS arm64 local smoke, keep `use_gpu=false`.
