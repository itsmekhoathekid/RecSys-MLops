#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
terraform_dir="${TERRAFORM_DIR:-infra/terraform/gcp}"
resource='helm_release.recsys_kagent_agent[0]'
backup_path="${KAGENT_STATE_BACKUP_PATH:?Set KAGENT_STATE_BACKUP_PATH to a secure path outside the repository}"

case "${backup_path}" in
  "$(pwd)"/*)
    echo "State backups must not be written inside the repository." >&2
    exit 2
    ;;
esac
[[ "${CONFIRM_KAGENT_OWNERSHIP_TRANSFER:-}" == "yes" ]] || {
  echo "Set CONFIRM_KAGENT_OWNERSHIP_TRANSFER=yes after reviewing terraform plan." >&2
  exit 2
}

umask 077
mkdir -p reports
terraform -chdir="${terraform_dir}" state list | grep -Fx "${resource}"
helm history recsys-kagent-agent -n kagent -o json >reports/kagent-agent-history-before.json
terraform -chdir="${terraform_dir}" state pull >"${backup_path}"
test -s "${backup_path}"
terraform -chdir="${terraform_dir}" state rm "${resource}"
if terraform -chdir="${terraform_dir}" state list | grep -Fxq "${resource}"; then
  echo "Terraform resource is still present after state rm." >&2
  exit 1
fi
helm history recsys-kagent-agent -n kagent -o json >reports/kagent-agent-history-after.json
python3 - <<'PY'
import json

before = json.load(open("reports/kagent-agent-history-before.json", encoding="utf-8"))
after = json.load(open("reports/kagent-agent-history-after.json", encoding="utf-8"))
assert before, "Helm release has no rollback history"
assert after == before, "Helm history changed during state-only ownership transfer"
PY
echo "Terraform ownership removed without deleting recsys-kagent-agent; Jenkins may now adopt the release."
