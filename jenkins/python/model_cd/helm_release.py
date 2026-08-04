from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


KSERVE_RELEASE = "recsys-serving"
KSERVE_NAMESPACE = "kserve-triton-inference"
KSERVE_CHART = "infra/helm/recsys-serving"
INFERENCE_RELEASE = "recsys-inference-api"
INFERENCE_NAMESPACE = "api-serving"
INFERENCE_CHART = "infra/helm/recsys-inference-api"


def run(command: list[str]) -> None:
    subprocess.check_call(command)


def crd_exists(name: str) -> bool:
    return (
        subprocess.run(
            ["kubectl", "get", "crd", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def _atomic_args() -> list[str]:
    enabled = os.getenv("RECSYS_MODEL_CD_ATOMIC", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    return ["--atomic"] if enabled else []


def _helm_upgrade(
    release: str,
    chart: str,
    namespace: str,
    values_path: Path,
    timeout: str,
    *extra: str,
    reset_values: bool = False,
) -> None:
    run(
        [
            "helm",
            "upgrade",
            "--install",
            release,
            chart,
            "--namespace",
            namespace,
            "--create-namespace",
            "--reset-values" if reset_values else "--reuse-values",
            "--cleanup-on-fail",
            "--wait",
            "--wait-for-jobs",
            "--history-max",
            os.getenv("HELM_HISTORY_MAX", "20"),
            "--timeout",
            timeout,
            "-f",
            str(values_path),
            *_atomic_args(),
            *extra,
        ]
    )


def _archive_values(release: str, namespace: str, target: Path) -> bool:
    completed = subprocess.run(
        ["helm", "get", "values", release, "-n", namespace, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return False
    json.loads(completed.stdout)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(completed.stdout, encoding="utf-8")
    return True


def _wait_stable(timeout: str) -> None:
    run(
        [
            "kubectl",
            "wait",
            "--for=condition=Ready",
            "inferenceservice/recsys-bst-triton",
            "-n",
            KSERVE_NAMESPACE,
            f"--timeout={timeout}",
        ]
    )
    run(
        [
            "kubectl",
            "wait",
            "--for=condition=Available",
            "deployment/recsys-bst-triton-predictor",
            "-n",
            KSERVE_NAMESPACE,
            f"--timeout={timeout}",
        ]
    )


def _wait_candidate(timeout: str) -> None:
    run(
        [
            "kubectl",
            "wait",
            "--for=condition=Ready",
            "inferenceservice/recsys-bst-triton-candidate",
            "-n",
            KSERVE_NAMESPACE,
            f"--timeout={timeout}",
        ]
    )
    run(
        [
            "kubectl",
            "wait",
            "--for=condition=Available",
            "deployment/recsys-bst-triton-candidate-predictor",
            "-n",
            KSERVE_NAMESPACE,
            f"--timeout={timeout}",
        ]
    )


def _wait_inference(timeout: str) -> None:
    run(
        [
            "kubectl",
            "rollout",
            "status",
            "deployment/recsys-inference-api",
            "-n",
            INFERENCE_NAMESPACE,
            f"--timeout={timeout}",
        ]
    )


def _deploy_kserve(values_path: Path, timeout: str, candidate: bool) -> None:
    run(["helm", "lint", KSERVE_CHART, "-f", str(values_path)])
    _helm_upgrade(
        KSERVE_RELEASE,
        KSERVE_CHART,
        KSERVE_NAMESPACE,
        values_path,
        timeout,
        "--set",
        "autoscaling.kserveResource.enabled=false",
    )
    _wait_stable(timeout)
    if candidate:
        _wait_candidate(timeout)
    _helm_upgrade(
        KSERVE_RELEASE,
        KSERVE_CHART,
        KSERVE_NAMESPACE,
        values_path,
        timeout,
        "--set",
        "autoscaling.kserveResource.enabled=true",
    )


def _deploy_inference(values_path: Path, timeout: str) -> None:
    run(["helm", "lint", INFERENCE_CHART, "-f", str(values_path)])
    _helm_upgrade(
        INFERENCE_RELEASE,
        INFERENCE_CHART,
        INFERENCE_NAMESPACE,
        values_path,
        timeout,
    )
    _wait_inference(timeout)


def deploy(
    values_path: Path,
    timeout: str,
    inference_values_path: Path | None = None,
    stage: str | None = None,
) -> None:
    kserve_values = json.loads(values_path.read_text(encoding="utf-8"))
    inference_values_path = inference_values_path or (
        values_path.parent / "recsys-inference-api-values.json"
    )
    if not inference_values_path.is_file():
        raise FileNotFoundError(f"missing inference values: {inference_values_path}")
    stage = stage or kserve_values.get("modelCd", {}).get("stage", "deploy")
    candidate = bool(
        kserve_values.get("kserve", {})
        .get("inferenceService", {})
        .get("retainCandidate", False)
    )

    archive_dir = values_path.parent / "pre-change"
    kserve_archive = archive_dir / "recsys-kserve-values.json"
    inference_archive = archive_dir / "recsys-inference-api-values.json"
    had_kserve = _archive_values(KSERVE_RELEASE, KSERVE_NAMESPACE, kserve_archive)
    had_inference = _archive_values(
        INFERENCE_RELEASE, INFERENCE_NAMESPACE, inference_archive
    )

    try:
        if stage == "ab-step":
            _wait_candidate(timeout)
            _deploy_inference(inference_values_path, timeout)
        elif stage == "rollback":
            _deploy_inference(inference_values_path, timeout)
            _deploy_kserve(values_path, timeout, candidate=False)
        else:
            _deploy_kserve(values_path, timeout, candidate=candidate)
            _deploy_inference(inference_values_path, timeout)
    except Exception:
        if had_inference:
            _helm_upgrade(
                INFERENCE_RELEASE,
                INFERENCE_CHART,
                INFERENCE_NAMESPACE,
                inference_archive,
                timeout,
                reset_values=True,
            )
        if had_kserve:
            _helm_upgrade(
                KSERVE_RELEASE,
                KSERVE_CHART,
                KSERVE_NAMESPACE,
                kserve_archive,
                timeout,
                reset_values=True,
            )
        raise
