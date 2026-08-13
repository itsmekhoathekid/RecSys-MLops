from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops/gcp/services_power.sh"


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fake_environment(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    cloud_state = tmp_path / "cloud-state"
    cloud_state.mkdir()
    for pool, nodes, minimum, maximum in (
        ("recsys-mlops-cpu", 1, 1, 2),
        ("recsys-mlops-ml-system", 1, 1, 1),
        ("recsys-mlops-gpu", 0, 0, 1),
        ("recsys-mlops-llm-cpu", 1, 1, 2),
    ):
        (cloud_state / f"{pool}.nodes").write_text(f"{nodes}\n", encoding="utf-8")
        (cloud_state / f"{pool}.min").write_text(f"{minimum}\n", encoding="utf-8")
        (cloud_state / f"{pool}.max").write_text(f"{maximum}\n", encoding="utf-8")

    _executable(
        fake_bin / "python3",
        """#!/usr/bin/env bash
case "${*: -1}" in
  projectId) printf '%s\n' recsys-mlops ;;
  zone) printf '%s\n' asia-southeast1-b ;;
  cluster) printf '%s\n' recsys-mlops-gke ;;
  *) exit 2 ;;
esac
""",
    )
    _executable(
        fake_bin / "gcloud",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'gcloud %s\n' "$*" >>"${FAKE_COMMAND_LOG}"
args=" $* "
if [[ "${args}" == *" container clusters get-credentials "* ]]; then
  exit 0
fi
if [[ "${args}" == *" container node-pools describe "* ]]; then
  pool="$4"
  if [[ -f "${FAKE_DESCRIBE_FATAL_FILE:-/nonexistent}" ]]; then
    printf '%s\n' 'ERROR: billing or authorization denied' >&2
    exit 17
  fi
  [[ -f "${FAKE_CLOUD_STATE}/${pool}.nodes" ]] || exit 1
  case "${args}" in
    *"currentNodeCount"*) cat "${FAKE_CLOUD_STATE}/${pool}.nodes" ;;
    *"autoscaling.minNodeCount"*) cat "${FAKE_CLOUD_STATE}/${pool}.min" ;;
    *"autoscaling.maxNodeCount"*) cat "${FAKE_CLOUD_STATE}/${pool}.max" ;;
  esac
  exit 0
fi
if [[ "${args}" == *" container node-pools update "* ]]; then
  exit 0
fi
if [[ "${args}" == *" container clusters resize "* ]]; then
  pool=""
  nodes=""
  while (($#)); do
    case "$1" in
      --node-pool) pool="$2"; shift 2 ;;
      --num-nodes) nodes="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  if [[ -f "${FAKE_FAIL_RESIZE_ONCE:-/nonexistent}" && "${pool}" == "$(cat "${FAKE_FAIL_RESIZE_ONCE}")" ]]; then
    rm -f "${FAKE_FAIL_RESIZE_ONCE}"
    exit 42
  fi
  printf '%s\n' "${nodes}" >"${FAKE_CLOUD_STATE}/${pool}.nodes"
  exit 0
fi
if [[ "${args}" == *" container node-pools list "* ]]; then
  printf '%s\n' 'NAME STATUS AUTOSCALING'
  exit 0
fi
exit 0
""",
    )
    _executable(
        fake_bin / "kubectl",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'kubectl %s\n' "$*" >>"${FAKE_COMMAND_LOG}"
args=" $* "
if [[ "${args}" == *" get pvc -A -o json "* ]]; then
  cat "${FAKE_PVC_JSON}"
  exit 0
fi
if [[ "${args}" == *" get pvc -A "* ]]; then
  printf '%s\n' 'NAMESPACE NAME STATUS VOLUME' 'data postgres Bound pv-postgres'
  exit 0
fi
if [[ "${args}" == *" get pdb -A -o json "* ]]; then
  printf '%s\n' '{"items":[]}'
  exit 0
fi
if [[ "${args}" == *" get nodes "* && "${args}" == *" -o name "* ]]; then
  exit 0
fi
if [[ "${args}" == *" get nodes "* ]]; then
  printf '%s\n' 'NAME STATUS'
  exit 0
fi
if [[ "${args}" == *" get deployment,statefulset,daemonset -A -o json "* ]]; then
  printf '%s\n' '{"items":[]}'
  exit 0
fi
if [[ "${args}" == *" get deployment,statefulset,daemonset -A "* ]]; then
  printf '%s\n' 'No resources found'
  exit 0
fi
if [[ "${args}" == *" get pods -A -o json "* ]]; then
  cat "${FAKE_PODS_JSON}"
  exit 0
fi
if [[ "${args}" == *" get pods -A "* ]]; then
  exit 0
fi
if [[ "${args}" == *" get namespace "* || "${args}" == *" get deploy "* || "${args}" == *" get endpoints "* || "${args}" == *" get inferenceservice "* ]]; then
  exit 1
fi
if [[ "${args}" == *" wait "* ]]; then
  exit 0
fi
exit 0
""",
    )

    pvc_json = tmp_path / "pvcs.json"
    pvc_json.write_text(
        '{"items":[{"metadata":{"namespace":"data","name":"postgres",'
        '"uid":"uid-postgres"},"spec":{"volumeName":"pv-postgres"}}]}\n',
        encoding="utf-8",
    )
    command_log = tmp_path / "commands.log"
    command_log.touch()
    pods_json = tmp_path / "pods.json"
    pods_json.write_text('{"items":[]}\n', encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_CLOUD_STATE": str(cloud_state),
            "FAKE_COMMAND_LOG": str(command_log),
            "FAKE_PVC_JSON": str(pvc_json),
            "FAKE_PODS_JSON": str(pods_json),
            "GCP_POWER_STATE_FILE": str(tmp_path / "power.env"),
            "GCP_POWER_PVC_STATE_FILE": str(tmp_path / "power.pvcs"),
            "GCP_SERVICES_INSTALL_CI": "0",
        }
    )
    return env


def _run(action: str, env: dict[str, str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), action],
        cwd=ROOT,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def test_up_down_status_are_repeatable_and_preserve_pvc_identity(tmp_path: Path) -> None:
    env = _fake_environment(tmp_path)
    state_file = Path(env["GCP_POWER_STATE_FILE"])
    pvc_state_file = Path(env["GCP_POWER_PVC_STATE_FILE"])
    cloud_state = Path(env["FAKE_CLOUD_STATE"])

    _run("down", env)
    first_snapshot = state_file.read_text(encoding="utf-8")
    assert "CPU_NODES=1" in first_snapshot
    assert "ML_NODES=1" in first_snapshot
    assert "LLM_CPU_NODES=1" in first_snapshot
    assert "HIBERNATING=1" in first_snapshot
    assert "uid-postgres" in pvc_state_file.read_text(encoding="utf-8")
    assert (cloud_state / "recsys-mlops-cpu.nodes").read_text().strip() == "0"
    assert (cloud_state / "recsys-mlops-llm-cpu.nodes").read_text().strip() == "0"

    _run("down", env)
    assert state_file.read_text(encoding="utf-8") == first_snapshot

    down_status = _run("status", env)
    assert "PVC identity check: OK" in down_status.stdout
    assert "HIBERNATING=1" in down_status.stdout

    _run("up", env)
    assert "HIBERNATING=0" in state_file.read_text(encoding="utf-8")
    assert (cloud_state / "recsys-mlops-cpu.nodes").read_text().strip() == "1"
    assert (cloud_state / "recsys-mlops-ml-system.nodes").read_text().strip() == "1"
    assert (cloud_state / "recsys-mlops-llm-cpu.nodes").read_text().strip() == "1"

    _run("up", env)
    up_status = _run("status", env)
    assert "PVC identity check: OK" in up_status.stdout
    assert "HIBERNATING=0" in up_status.stdout

    _run("down", env)
    assert "HIBERNATING=1" in state_file.read_text(encoding="utf-8")


def test_interrupted_down_keeps_original_pre_hibernate_snapshot(tmp_path: Path) -> None:
    env = _fake_environment(tmp_path)
    fail_once = tmp_path / "fail-resize-once"
    fail_once.write_text("recsys-mlops-ml-system\n", encoding="utf-8")
    env["FAKE_FAIL_RESIZE_ONCE"] = str(fail_once)

    failed = _run("down", env, check=False)
    assert failed.returncode == 1

    state_file = Path(env["GCP_POWER_STATE_FILE"])
    interrupted_snapshot = state_file.read_text(encoding="utf-8")
    assert "CPU_NODES=1" in interrupted_snapshot
    assert "ML_NODES=1" in interrupted_snapshot
    assert "HIBERNATING=0" in interrupted_snapshot
    assert "restoring autoscaling" in failed.stderr
    assert "Hibernate rollback complete" in failed.stderr
    cloud_state = Path(env["FAKE_CLOUD_STATE"])
    assert (cloud_state / "recsys-mlops-cpu.nodes").read_text().strip() == "1"
    assert (cloud_state / "recsys-mlops-ml-system.nodes").read_text().strip() == "1"

    _run("down", env)
    assert "HIBERNATING=1" in state_file.read_text(encoding="utf-8")


def test_up_fails_closed_when_a_pvc_uid_or_binding_changes(tmp_path: Path) -> None:
    env = _fake_environment(tmp_path)
    _run("down", env)

    Path(env["FAKE_PVC_JSON"]).write_text(
        '{"items":[{"metadata":{"namespace":"data","name":"postgres",'
        '"uid":"replacement-uid"},"spec":{"volumeName":"replacement-pv"}}]}\n',
        encoding="utf-8",
    )
    failed = _run("up", env, check=False)

    assert failed.returncode != 0
    assert "PVC identity check failed" in failed.stderr
    assert "HIBERNATING=1" in Path(env["GCP_POWER_STATE_FILE"]).read_text(encoding="utf-8")


def test_historical_batch_pods_do_not_hide_unhealthy_service_pods(tmp_path: Path) -> None:
    env = _fake_environment(tmp_path)
    _run("down", env)

    pods_json = Path(env["FAKE_PODS_JSON"])
    pods_json.write_text(
        '{"items":[{"metadata":{"namespace":"data","name":"old-airflow-task"},'
        '"status":{"phase":"Error"}}]}\n',
        encoding="utf-8",
    )
    ignored_batch = _run("up", env)
    assert "retained batch/workflow pods" in ignored_batch.stdout

    pods_json.write_text(
        '{"items":[{"metadata":{"namespace":"api","name":"api-pod",'
        '"ownerReferences":[{"kind":"ReplicaSet","name":"api-rs"}]},'
        '"status":{"phase":"Pending"}}]}\n',
        encoding="utf-8",
    )
    failed_service = _run("up", env, check=False)
    assert failed_service.returncode != 0
    assert "api-pod" in failed_service.stdout


def test_cloud_api_error_is_not_misreported_as_a_missing_node_pool(tmp_path: Path) -> None:
    env = _fake_environment(tmp_path)
    fatal_file = tmp_path / "describe-fatal"
    fatal_file.touch()
    env["FAKE_DESCRIBE_FATAL_FILE"] = str(fatal_file)

    failed = _run("down", env, check=False)

    assert failed.returncode == 17
    assert "refusing to treat an API/auth/billing error as a missing pool" in failed.stderr
    assert "Skip CPU" not in failed.stdout


def test_snapshot_from_another_project_cannot_override_production_target(tmp_path: Path) -> None:
    env = _fake_environment(tmp_path)
    _run("down", env)
    state_file = Path(env["GCP_POWER_STATE_FILE"])
    state_file.write_text(
        state_file.read_text(encoding="utf-8").replace(
            "STATE_PROJECT_ID=recsys-mlops", "STATE_PROJECT_ID=fsds-coursework"
        ),
        encoding="utf-8",
    )

    failed = _run("up", env, check=False)

    assert failed.returncode == 2
    assert "Power snapshot target mismatch" in failed.stderr
    assert "configured: recsys-mlops" in failed.stderr


def test_up_normalizes_istiod_without_weakening_mesh_readiness(tmp_path: Path) -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    terraform = (ROOT / "infra/terraform/gcp/dependencies.tf").read_text(
        encoding="utf-8"
    )

    assert "normalize_istiod_control_plane" in script
    assert 'ISTIOD_CPU_REQUEST:-50m' in script
    assert 'ISTIOD_MEMORY_REQUEST:-256Mi' in script
    assert "scale_deploy_if_exists istio-system istiod 1" in script
    assert 'name  = "pilot.resources.requests.cpu"' in terraform
    assert 'value = "50m"' in terraform
    assert 'name  = "pilot.resources.requests.memory"' in terraform
