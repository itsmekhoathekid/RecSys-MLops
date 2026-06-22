from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


GROUP = "ray.io"
VERSION = "v1"
PLURAL = "rayjobs"


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
            {"name": "RAY_DASHBOARD_SUBPROCESS_MODULE_WAIT_READY_TIMEOUT", "value": "120"},
            {"name": "RAY_USAGE_STATS_ENABLED", "value": "0"},
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
    return {"spec": spec}


def build_rayjob(args: argparse.Namespace) -> dict[str, Any]:
    ray_args = [
        "python",
        "/opt/recsys/apps/ml-system/src/ray_tune_train_bst.py",
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
    head = container_spec(
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
    )
    worker = container_spec(
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
    )
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "RayJob",
        "metadata": {
            "name": args.job_name,
            "namespace": args.namespace,
            "labels": {"app.kubernetes.io/name": "recsys-ray-tune"},
        },
        "spec": {
            "entrypoint": " ".join(ray_args),
            "shutdownAfterJobFinishes": True,
            "ttlSecondsAfterFinished": args.ttl_seconds_after_finished,
            "rayClusterSpec": {
                "headGroupSpec": {
                    "serviceType": "ClusterIP",
                    "rayStartParams": {
                        "dashboard-host": "0.0.0.0",
                        "num-cpus": "0",
                    },
                    "template": pod_template(head, args.pvc_name, use_gpu=False),
                },
                "workerGroupSpecs": [
                    {
                        "groupName": "cpu-or-gpu-workers",
                        "replicas": args.worker_replicas,
                        "minReplicas": args.worker_replicas,
                        "maxReplicas": args.worker_replicas,
                        "rayStartParams": {},
                        "template": pod_template(worker, args.pvc_name, use_gpu=args.use_gpu),
                    }
                ],
            },
        },
    }


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


def wait_for_completion(api, namespace: str, name: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_status: dict[str, Any] = {}
    while time.time() < deadline:
        payload = api.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit and wait for a KubeRay RayJob")
    parser.add_argument("--namespace", default="kubeflow")
    parser.add_argument("--job-name", default="recsys-bst-ray-tune")
    parser.add_argument("--image", default="recsys-mlops-training:local")
    parser.add_argument("--pvc-name", default="recsys-mlops-pvc")
    parser.add_argument("--runtime-secret-name", default="recsys-mlops-runtime")
    parser.add_argument("--base-config-path", default="/opt/recsys/configs/local/bst.yaml")
    parser.add_argument("--split-dir", default="/workspace/recsys/notebooks/data/bst_split")
    parser.add_argument("--ray-output-dir", default="/workspace/recsys/data_platform/output/ml/ray")
    parser.add_argument("--best-result-path", default="/workspace/recsys/data_platform/output/ml/ray/best_result.json")
    parser.add_argument("--training-percent", type=float, default=0.01)
    parser.add_argument("--num-epochs", type=int, default=1)
    parser.add_argument("--max-trials", type=int, default=2)
    parser.add_argument("--parallel-trials", type=int, default=1)
    parser.add_argument("--cpus-per-trial", type=float, default=1.0)
    parser.add_argument("--gpus-per-trial", type=float, default=0.0)
    parser.add_argument("--worker-replicas", type=int, default=1)
    parser.add_argument("--head-cpu-request", default="500m")
    parser.add_argument("--head-cpu-limit", default="1")
    parser.add_argument("--head-memory-request", default="1Gi")
    parser.add_argument("--head-memory-limit", default="2Gi")
    parser.add_argument("--worker-cpu-request", default="1")
    parser.add_argument("--worker-cpu-limit", default="2")
    parser.add_argument("--worker-memory-request", default="2Gi")
    parser.add_argument("--worker-memory-limit", default="4Gi")
    parser.add_argument("--use-gpu", action="store_true")
    parser.add_argument("--use-gpu-value", default="false")
    parser.add_argument("--gpu-limit", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--ttl-seconds-after-finished", type=int, default=300)
    parser.add_argument("--status-path", default="")
    args = parser.parse_args()
    args.use_gpu = args.use_gpu or args.use_gpu_value.lower() in {"1", "true", "yes"}

    api = load_kubernetes()
    delete_if_exists(api, args.namespace, args.job_name)
    rayjob = build_rayjob(args)
    api.create_namespaced_custom_object(GROUP, VERSION, args.namespace, PLURAL, rayjob)
    status = wait_for_completion(api, args.namespace, args.job_name, args.timeout_seconds)
    if args.status_path:
        Path(args.status_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.status_path).write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
