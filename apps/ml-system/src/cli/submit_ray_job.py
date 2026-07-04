from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


GROUP = "ray.io"
VERSION = "v1"
PLURAL = "rayjobs"
TUNE_ENTRYPOINT = "/opt/recsys/apps/ml-system/src/training/ray_tune_train_bst.py"
DDP_ENTRYPOINT = "/opt/recsys/apps/ml-system/src/training/ray_distributed_train_bst.py"


def int_arg(value: str) -> int:
    return int(float(value))


def load_kubernetes():
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CustomObjectsApi()


def container_spec(
    name: str,
    image: str,
    cpu_request: str,
    cpu_limit: str,
    memory_request: str,
    memory_limit: str,
    pvc_name: str,
    runtime_secret_name: str,
    use_gpu: bool,
    gpu_limit: int,
) -> dict[str, Any]:
    resources: dict[str, Any] = {
        "requests": {"cpu": cpu_request, "memory": memory_request},
        "limits": {"cpu": cpu_limit, "memory": memory_limit},
    }
    if use_gpu and gpu_limit > 0:
        resources["requests"]["nvidia.com/gpu"] = gpu_limit
        resources["limits"]["nvidia.com/gpu"] = gpu_limit

    return {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "resources": resources,
        "env": [
            {"name": "RAY_DASHBOARD_SUBPROCESS_MODULE_WAIT_READY_TIMEOUT", "value": "300"},
            {"name": "RAY_USAGE_STATS_ENABLED", "value": "0"},
            {"name": "RAY_memory_usage_threshold", "value": "0.99"},
        ],
        "envFrom": [{"secretRef": {"name": runtime_secret_name}}],
        "volumeMounts": [{"name": "recsys-workspace", "mountPath": "/workspace"}],
    }


def pod_template(
    container: dict[str, Any],
    pvc_name: str,
    use_gpu: bool,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "containers": [container],
        "volumes": [
            {
                "name": "recsys-workspace",
                "persistentVolumeClaim": {"claimName": pvc_name},
            }
        ],
    }
    if use_gpu:
        spec["nodeSelector"] = {"nvidia.com/gpu.present": "true"}
        spec["tolerations"] = [
            {
                "key": "nvidia.com/gpu",
                "operator": "Exists",
                "effect": "NoSchedule",
            }
        ]
    return {
        "metadata": {
            "annotations": {
                "sidecar.istio.io/inject": "false",
            }
        },
        "spec": spec,
    }


def build_ray_args(args: argparse.Namespace) -> list[str]:
    job_mode = getattr(args, "job_mode", "tune")
    if job_mode == "distributed-train":
        ray_args = [
            "python",
            DDP_ENTRYPOINT,
            "--base-config-path",
            args.base_config_path,
            "--split-dir",
            args.split_dir,
            "--output-dir",
            args.ray_output_dir,
            "--training-percent",
            str(args.training_percent),
            "--num-epochs",
            str(args.num_epochs),
            "--num-workers",
            str(max(1, int(getattr(args, "num_workers", args.worker_replicas)))),
            "--cpus-per-worker",
            str(args.cpus_per_trial),
            "--gpus-per-worker",
            str(args.gpus_per_trial if args.use_gpu else 0),
            "--tune-result-path",
            args.tune_result_path,
            "--best-result-path",
            args.best_result_path,
        ]
    else:
        ray_args = [
            "python",
            TUNE_ENTRYPOINT,
            "--base-config-path",
            args.base_config_path,
            "--split-dir",
            args.split_dir,
            "--output-dir",
            args.ray_output_dir,
            "--training-percent",
            str(args.training_percent),
            "--num-epochs",
            str(args.num_epochs),
            "--max-trials",
            str(args.max_trials),
            "--parallel-trials",
            str(args.parallel_trials),
            "--cpus-per-trial",
            str(args.cpus_per_trial),
            "--gpus-per-trial",
            str(args.gpus_per_trial if args.use_gpu else 0),
            "--best-result-path",
            args.best_result_path,
        ]
    if getattr(args, "dataset_metadata_path", ""):
        ray_args.extend(["--dataset-metadata-path", args.dataset_metadata_path])
    return ray_args


def build_rayjob(args: argparse.Namespace) -> dict[str, Any]:
    ray_args = build_ray_args(args)
    job_mode = getattr(args, "job_mode", "tune")
    app_name = "recsys-ray-ddp-train" if job_mode == "distributed-train" else "recsys-ray-tune"
    rayjob = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "RayJob",
        "metadata": {
            "name": args.job_name,
            "namespace": args.namespace,
            "labels": {"app.kubernetes.io/name": app_name, "recsys.ai/ray-job-mode": job_mode},
            "annotations": {"sidecar.istio.io/inject": "false"},
        },
        "spec": {
            "entrypoint": " ".join(ray_args),
            "shutdownAfterJobFinishes": True,
            "ttlSecondsAfterFinished": args.ttl_seconds_after_finished,
            "submitterPodTemplate": {
                "metadata": {
                    "annotations": {"sidecar.istio.io/inject": "false"},
                },
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "ray-job-submitter",
                            "image": args.image,
                            "imagePullPolicy": "IfNotPresent",
                            "resources": {
                                "requests": {
                                    "cpu": getattr(args, "submitter_cpu_request", "50m"),
                                    "memory": getattr(args, "submitter_memory_request", "128Mi"),
                                },
                                "limits": {
                                    "cpu": getattr(args, "submitter_cpu_limit", "500m"),
                                    "memory": getattr(args, "submitter_memory_limit", "512Mi"),
                                },
                            },
                        }
                    ],
                },
            },
            "rayClusterSpec": {
                "headGroupSpec": {
                    "serviceType": "ClusterIP",
                    "rayStartParams": {
                        "dashboard-host": "0.0.0.0",
                        "num-cpus": args.head_ray_num_cpus,
                        "memory": args.head_ray_memory_bytes,
                        "object-store-memory": args.head_object_store_memory_bytes,
                    },
                    "template": pod_template(
                        container_spec(
                            name="ray-head",
                            image=args.image,
                            cpu_request=args.head_cpu_request,
                            cpu_limit=args.head_cpu_limit,
                            memory_request=args.head_memory_request,
                            memory_limit=args.head_memory_limit,
                            pvc_name=args.pvc_name,
                            runtime_secret_name=args.runtime_secret_name,
                            use_gpu=False,
                            gpu_limit=0,
                        ),
                        args.pvc_name,
                        use_gpu=False,
                    ),
                },
                "workerGroupSpecs": [],
            },
        },
    }
    if args.worker_replicas > 0:
        rayjob["spec"]["rayClusterSpec"]["workerGroupSpecs"].append(
            {
                "groupName": "cpu-or-gpu-workers",
                "replicas": args.worker_replicas,
                "minReplicas": args.worker_replicas,
                "maxReplicas": args.worker_replicas,
                "rayStartParams": {
                    "memory": args.worker_ray_memory_bytes,
                    "object-store-memory": args.worker_object_store_memory_bytes,
                },
                "template": pod_template(
                    container_spec(
                        name="ray-worker",
                        image=args.image,
                        cpu_request=args.worker_cpu_request,
                        cpu_limit=args.worker_cpu_limit,
                        memory_request=args.worker_memory_request,
                        memory_limit=args.worker_memory_limit,
                        pvc_name=args.pvc_name,
                        runtime_secret_name=args.runtime_secret_name,
                        use_gpu=args.use_gpu,
                        gpu_limit=args.gpu_limit,
                    ),
                    args.pvc_name,
                    use_gpu=args.use_gpu,
                ),
            }
        )
    return rayjob


def delete_if_exists(api, namespace: str, name: str) -> None:
    try:
        api.delete_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
    except Exception as exc:
        if getattr(exc, "status", None) != 404:
            raise
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            api.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return
            raise
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for existing RayJob {name} to be deleted")


def create_rayjob(api, namespace: str, name: str, rayjob: dict[str, Any]) -> None:
    deadline = time.time() + 120
    while True:
        try:
            api.create_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, rayjob)
            return
        except Exception as exc:
            if getattr(exc, "status", None) != 409 or time.time() >= deadline:
                raise
            print(json.dumps({"rayJobCreateConflict": name, "retryInSeconds": 5}))
            time.sleep(5)


def wait_for_completion(api, namespace: str, name: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_status: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            payload = api.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
        except Exception as exc:
            if getattr(exc, "status", None) in {500, 502, 503, 504}:
                print(json.dumps({"transientKubernetesApiError": getattr(exc, "status", None)}))
                time.sleep(10)
                continue
            raise
        last_status = payload.get("status", {})
        job_status = last_status.get("jobStatus")
        deployment_status = last_status.get("jobDeploymentStatus")
        print(json.dumps({"jobStatus": job_status, "jobDeploymentStatus": deployment_status}))
        if job_status == "SUCCEEDED":
            return last_status
        if job_status in {"FAILED", "STOPPED"}:
            raise RuntimeError(f"RayJob {name} failed with status: {last_status}")
        time.sleep(10)
    raise TimeoutError(f"Timed out waiting for RayJob {name}; last status: {last_status}")


def _load_json(path: str) -> dict[str, Any] | None:
    if not path:
        return None
    target = Path(path)
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _dataset_versions_match(best_result: dict[str, Any], dataset_metadata: dict[str, Any] | None) -> bool:
    if not dataset_metadata:
        return True
    expected = dataset_metadata.get("splits", {})
    actual = best_result.get("dataset_versions", {})
    if not expected or not actual:
        return False
    for split, expected_version in expected.items():
        actual_version = actual.get(split)
        if not actual_version:
            return False
        for key in ("table", "snapshot_id", "commit_time", "tag", "row_count"):
            if actual_version.get(key) != expected_version.get(key):
                return False
    return True


def reusable_best_result(best_result_path: str, dataset_metadata_path: str = "") -> dict[str, Any] | None:
    best_result = _load_json(best_result_path)
    if not best_result or not best_result.get("mlflow_run_id"):
        return None
    checkpoint_path = best_result.get("checkpoint_path")
    if checkpoint_path and not Path(checkpoint_path).exists():
        return None
    dataset_metadata = _load_json(dataset_metadata_path)
    if not _dataset_versions_match(best_result, dataset_metadata):
        return None
    return best_result


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit and wait for a KubeRay RayJob")
    parser.add_argument("--pipeline-run-id", default="")
    parser.add_argument("--namespace", default="kubeflow")
    parser.add_argument("--job-name", default="recsys-bst-ray-tune")
    parser.add_argument("--job-mode", choices=["tune", "distributed-train"], default="tune")
    parser.add_argument("--image", default="recsys-mlops-training:local")
    parser.add_argument("--pvc-name", default="recsys-mlops-pvc")
    parser.add_argument("--runtime-secret-name", default="recsys-mlops-runtime")
    parser.add_argument("--base-config-path", default="/opt/recsys/configs/local/bst.yaml")
    parser.add_argument("--split-dir", default="/workspace/recsys/data_platform/output/ml/bst_split")
    parser.add_argument("--ray-output-dir", default="/workspace/recsys/data_platform/output/ml/ray")
    parser.add_argument("--best-result-path", default="/workspace/recsys/data_platform/output/ml/ray/best_result.json")
    parser.add_argument("--tune-result-path", default="/workspace/recsys/data_platform/output/ml/ray/tune_result.json")
    parser.add_argument("--dataset-metadata-path", default="")
    parser.add_argument("--training-percent", type=float, default=0.01)
    parser.add_argument("--num-epochs", type=int_arg, default=1)
    parser.add_argument("--max-trials", type=int_arg, default=2)
    parser.add_argument("--parallel-trials", type=int_arg, default=1)
    parser.add_argument("--cpus-per-trial", type=float, default=1.0)
    parser.add_argument("--gpus-per-trial", type=float, default=0.0)
    parser.add_argument("--worker-replicas", type=int_arg, default=1)
    parser.add_argument("--num-workers", type=int_arg, default=0)
    parser.add_argument("--head-ray-num-cpus", default="0")
    parser.add_argument("--head-cpu-request", default="100m")
    parser.add_argument("--head-cpu-limit", default="2")
    parser.add_argument("--head-memory-request", default="768Mi")
    parser.add_argument("--head-memory-limit", default="3Gi")
    parser.add_argument("--head-ray-memory-bytes", default="1073741824")
    parser.add_argument("--head-object-store-memory-bytes", default="268435456")
    parser.add_argument("--worker-cpu-request", default="100m")
    parser.add_argument("--worker-cpu-limit", default="2")
    parser.add_argument("--worker-memory-request", default="1Gi")
    parser.add_argument("--worker-memory-limit", default="3Gi")
    parser.add_argument("--worker-ray-memory-bytes", default="1073741824")
    parser.add_argument("--worker-object-store-memory-bytes", default="268435456")
    parser.add_argument("--submitter-cpu-request", default="50m")
    parser.add_argument("--submitter-cpu-limit", default="500m")
    parser.add_argument("--submitter-memory-request", default="128Mi")
    parser.add_argument("--submitter-memory-limit", default="512Mi")
    parser.add_argument("--use-gpu", action="store_true")
    parser.add_argument("--use-gpu-value", default="false")
    parser.add_argument("--gpu-limit", type=int_arg, default=1)
    parser.add_argument("--timeout-seconds", type=int_arg, default=3600)
    parser.add_argument("--ttl-seconds-after-finished", type=int_arg, default=300)
    parser.add_argument("--status-path", default="")
    args = parser.parse_args()
    args.use_gpu = args.use_gpu or args.use_gpu_value.lower() in {"1", "true", "yes"}
    if args.num_workers <= 0:
        args.num_workers = max(1, args.worker_replicas)

    existing_result = reusable_best_result(args.best_result_path, args.dataset_metadata_path)
    if existing_result:
        status = {
            "jobStatus": "SUCCEEDED",
            "jobDeploymentStatus": "ReusedExistingResult",
            "bestResultPath": args.best_result_path,
            "mlflow_run_id": existing_result.get("mlflow_run_id"),
        }
        if args.status_path:
            Path(args.status_path).parent.mkdir(parents=True, exist_ok=True)
            Path(args.status_path).write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(status, sort_keys=True))
        return 0

    api = load_kubernetes()
    delete_if_exists(api, args.namespace, args.job_name)
    rayjob = build_rayjob(args)
    create_rayjob(api, args.namespace, args.job_name, rayjob)
    status = wait_for_completion(api, args.namespace, args.job_name, args.timeout_seconds)
    if args.status_path:
        Path(args.status_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.status_path).write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
