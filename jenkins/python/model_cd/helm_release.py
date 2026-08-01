from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

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


def deploy(values_path: Path, timeout: str) -> None:
    rendered_values = json.loads(values_path.read_text(encoding="utf-8"))
    candidate_uri = rendered_values.get("kserve", {}).get("inferenceService", {}).get("candidateStorageUri", "")
    candidate_requested = bool(candidate_uri) and (
        rendered_values.get("abTest", {}).get("enabled", False)
        or rendered_values.get("shadow", {}).get("enabled", False)
        or rendered_values.get("kserve", {})
        .get("inferenceService", {})
        .get("retainCandidate", False)
    )
    run(["helm", "lint", "infra/helm/recsys-serving", "-f", str(values_path)])
    bootstrap_set_args = ["--set", "autoscaling.kserveResource.enabled=false"]
    final_set_args = ["--set", "autoscaling.kserveResource.enabled=true"]
    if not crd_exists("servicemonitors.monitoring.coreos.com"):
        bootstrap_set_args.extend(["--set", "observability.serviceMonitor.enabled=false"])
        final_set_args.extend(["--set", "observability.serviceMonitor.enabled=false"])
    atomic_enabled = os.getenv("RECSYS_MODEL_CD_ATOMIC", "1").lower() not in {"0", "false", "no"}
    base_command = [
        "helm",
        "upgrade",
        "--install",
        "recsys-serving",
        "infra/helm/recsys-serving",
        "--namespace",
        "kserve-triton-inference",
        "--create-namespace",
        "--reuse-values",
        "--cleanup-on-fail",
        "--wait",
        "--wait-for-jobs",
        "--history-max",
        os.getenv("HELM_HISTORY_MAX", "20"),
        "--timeout",
        timeout,
        "-f",
        str(values_path),
    ]
    if atomic_enabled:
        base_command.append("--atomic")
    run(base_command + bootstrap_set_args)
    run(
        [
            "kubectl",
            "wait",
            "--for=condition=Ready",
            "inferenceservice/recsys-bst-triton",
            "-n",
            "kserve-triton-inference",
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
            "kserve-triton-inference",
            f"--timeout={timeout}",
        ]
    )
    if candidate_requested:
        run(
            [
                "kubectl",
                "wait",
                "--for=condition=Ready",
                "inferenceservice/recsys-bst-triton-candidate",
                "-n",
                "kserve-triton-inference",
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
                "kserve-triton-inference",
                f"--timeout={timeout}",
            ]
        )
    run(base_command + final_set_args)
