from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops/gcp/terraform_gcp.sh"


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _environment(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.log"
    command_log.touch()

    _executable(
        fake_bin / "gcloud",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'gcloud %s\n' "$*" >>"${FAKE_COMMAND_LOG}"
case " $* " in
  *" auth list "*) printf '%s\n' "${FAKE_ACTIVE_ACCOUNT}" ;;
  *" config get-value project "*) printf '%s\n' "${FAKE_ACTIVE_PROJECT}" ;;
  *" auth application-default print-access-token "*) printf '%s\n' fake-token ;;
  *) exit 17 ;;
esac
""",
    )
    _executable(
        fake_bin / "terraform",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'TF_DATA_DIR=%s\n' "${TF_DATA_DIR}"
printf 'GOOGLE_APPLICATION_CREDENTIALS=%s\n' "${GOOGLE_APPLICATION_CREDENTIALS:-<unset>}"
printf 'terraform %s\n' "$*"
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_COMMAND_LOG": str(command_log),
            "FAKE_ACTIVE_ACCOUNT": "new-admin@example.com",
            "FAKE_ACTIVE_PROJECT": "recsys-mlops-506406",
        }
    )
    for name in (
        "GCP_ACCOUNT",
        "GCP_PROJECT_ID",
        "GCP_TERRAFORM_CREDENTIAL_FILE",
        "GCP_TERRAFORM_DATA_DIR",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        env.pop(name, None)
    return env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "validate"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_account_is_required(tmp_path: Path) -> None:
    result = _run(_environment(tmp_path))

    assert result.returncode == 2
    assert "GCP_ACCOUNT must name the expected active account" in result.stderr


def test_wrong_active_account_is_rejected(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    env["GCP_ACCOUNT"] = "different@example.com"

    result = _run(env)

    assert result.returncode == 2
    assert "active account new-admin@example.com is not different@example.com" in result.stderr


def test_wrong_active_project_is_rejected(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    env["GCP_ACCOUNT"] = "new-admin@example.com"
    env["FAKE_ACTIVE_PROJECT"] = "wrong-project"

    result = _run(env)

    assert result.returncode == 2
    assert "active project wrong-project is not recsys-mlops-506406" in result.stderr


def test_standard_adc_and_project_scoped_data_directory_are_used(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    env["GCP_ACCOUNT"] = "new-admin@example.com"

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert "GOOGLE_APPLICATION_CREDENTIALS=<unset>" in result.stdout
    assert "TF_DATA_DIR=" in result.stdout
    assert "/.terraform/recsys-mlops-506406-gcs" in result.stdout
    assert "terraform validate" in result.stdout
    command_log = Path(env["FAKE_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert "gcloud auth application-default print-access-token" in command_log


def test_explicit_credential_file_overrides_standard_adc(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    credential_file = tmp_path / "terraform-adc.json"
    credential_file.write_text("{}\n", encoding="utf-8")
    env.update(
        {
            "GCP_ACCOUNT": "new-admin@example.com",
            "GCP_TERRAFORM_CREDENTIAL_FILE": str(credential_file),
        }
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert f"GOOGLE_APPLICATION_CREDENTIALS={credential_file}" in result.stdout
    command_log = Path(env["FAKE_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert "application-default print-access-token" not in command_log
