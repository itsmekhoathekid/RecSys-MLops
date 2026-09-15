#!/usr/bin/env bash
# Deploy shared settings and refresh their three Helm-owned consumers.
# Immutable rec-ab-* releases are deliberately outside this operation.
set -euo pipefail
test "${RECSYS_GLOBAL_CONFIG_LOCKED:-}" = "1" || { echo "Run through the Jenkins global-config job" >&2; exit 2; }
python3 -m jenkins.python.llm_agent_cd.release_guard
source jenkins/scripts/lib/common.sh

values_file="${1:-infra/helm/recsys-global-model-config/values.yaml}"
timeout="${HELM_TIMEOUT:-600s}"
model_config="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["modelConfig"]["name"])' "${values_file}")"
if [[ "${model_config}" == "default-model-config" ]]; then
  recsys_error "default-model-config belongs to the platform kagent release"
  exit 1
fi

# Check all consumer releases exist before making any changes.
for release in recsys-kagent-agent recsys-recommendation-agent recsys-coordinator-agent; do
  helm status "${release}" -n kagent >/dev/null
done
helm upgrade --install recsys-global-model-config infra/helm/recsys-global-model-config \
  -n kagent -f "${values_file}" --reset-values --atomic --wait --timeout "${timeout}"
for role in context recommendation coordinator; do
  release="recsys-${role}-agent"
  [[ "${role}" == "context" ]] && release=recsys-kagent-agent
  agent="recsys-${role}-agent-sandbox"
  # Retain only operator values, including the Recommendation router
  # activation. Reset chart defaults so a reviewed prompt/tool revision is
  # not silently replaced by computed values from the previous chart.
  operator_values="$(helm get values "${release}" -n kagent -o json)"
  helm upgrade "${release}" "infra/helm/${release}" -n kagent \
    --reset-values -f - --set-string "sandbox.modelConfig=${model_config}" \
    --atomic --wait --timeout "${timeout}" <<<"${operator_values}"
  kubectl -n kagent wait --for=condition=Ready "sandboxagent/${agent}" --timeout="${timeout}"
done
