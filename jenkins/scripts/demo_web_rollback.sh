#!/usr/bin/env bash
set -euo pipefail

release="${DEMO_WEB_RELEASE:-recsys-demo-web}"
namespace="${DEMO_WEB_NAMESPACE:-api-serving}"
timeout="${COMPONENT_DEPLOY_TIMEOUT:-600s}"
target_revision="${TARGET_REVISION:-}"

if [[ -z "${target_revision}" ]]; then
  target_revision="$(helm history "${release}" -n "${namespace}" -o json \
    | python3 -c 'import json,sys; rows=[r for r in json.load(sys.stdin) if r.get("status") in {"deployed","superseded"}]; current=max(int(r["revision"]) for r in rows); candidates=[int(r["revision"]) for r in rows if int(r["revision"])<current]; print(max(candidates) if candidates else "")')"
fi
if [[ -z "${target_revision}" ]]; then
  echo "No previous Helm revision exists for ${release}." >&2
  exit 2
fi

helm rollback "${release}" "${target_revision}" -n "${namespace}" --wait --timeout "${timeout}"
kubectl rollout status deployment/recsys-demo-web -n "${namespace}" --timeout="${timeout}"
kubectl rollout status deployment/recsys-demo-api -n "${namespace}" --timeout="${timeout}"
bash jenkins/scripts/demo_web_smoke.sh
echo "Rolled ${release} back to revision ${target_revision}."
