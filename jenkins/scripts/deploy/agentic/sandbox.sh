#!/usr/bin/env bash

sandbox_agent_model_revision() {
  local agent_name="$1"
  kubectl -n kagent get sandboxagent "${agent_name}" \
    -o jsonpath='{.metadata.annotations.recsys\.ai/model-config-revision}' \
    2>/dev/null || true
}

sandbox_agent_rebuild_golden_if_revision_changed() {
  local agent_name="$1"
  local previous_revision="$2"
  local current_revision template_name candidate candidate_uid ready
  local old_uids=" "
  local -a old_templates=()
  local -a candidates=()
  local attempts="${SANDBOX_AGENT_GOLDEN_REBUILD_ATTEMPTS:-120}"

  current_revision="$(sandbox_agent_model_revision "${agent_name}")"
  [[ -n "${current_revision}" ]] || {
    recsys_error "${agent_name} has no recsys.ai/model-config-revision"
    return 1
  }
  if [[ -z "${previous_revision}" || "${previous_revision}" == "${current_revision}" ]]; then
    return 0
  fi

  while IFS= read -r template_name; do
    [[ -n "${template_name}" ]] && old_templates+=("${template_name}")
  done < <(
    kubectl -n kagent get actortemplate \
      -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' \
      | grep -E "^${agent_name}-" || true
  )
  [[ "${#old_templates[@]}" -gt 0 ]] || {
    recsys_error "ActorTemplate for ${agent_name} was not created"
    return 1
  }
  for template_name in "${old_templates[@]}"; do
    candidate_uid="$(
      kubectl -n kagent get actortemplate "${template_name}" \
        -o jsonpath='{.metadata.uid}'
    )"
    old_uids+="${candidate_uid} "
  done
  # Every ActorTemplate with this SandboxAgent prefix is controller-owned.
  # Remove all stale generations together; otherwise an older Ready template
  # can make the rebuild gate pass before the desired revision is compiled.
  kubectl -n kagent delete actortemplate "${old_templates[@]}" --wait=true

  for _ in $(seq 1 "${attempts}"); do
    candidates=()
    while IFS= read -r candidate; do
      [[ -n "${candidate}" ]] && candidates+=("${candidate}")
    done < <(
      kubectl -n kagent get actortemplate \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' \
        | grep -E "^${agent_name}-" || true
    )
    for candidate in "${candidates[@]}"; do
      candidate_uid="$(
        kubectl -n kagent get actortemplate "${candidate}" \
          -o jsonpath='{.metadata.uid}'
      )"
      ready="$(
        kubectl -n kagent get actortemplate "${candidate}" \
          -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'
      )"
      if [[ "${old_uids}" != *" ${candidate_uid} "* && "${ready}" == "True" ]]; then
        kubectl -n kagent wait --for=condition=Ready \
          "sandboxagent/${agent_name}" --timeout="${timeout}"
        return 0
      fi
    done
    sleep 5
  done
  recsys_error "${agent_name} golden snapshot did not rebuild for revision ${current_revision}"
  return 1
}
