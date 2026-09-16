#!/usr/bin/env bash

# arctl and port-forward transport only. Registry contract semantics stay in
# jenkins.python.agent_registry_release.
agentic_registry_port_forward_pid=""

agentic_registry_open_tunnel() {
  local local_port="${AGENT_REGISTRY_LOCAL_PORT:-12121}"
  local log_file="reports/agentic/agentregistry-port-forward.log"
  mkdir -p reports/agentic
  [[ -z "${agentic_registry_port_forward_pid}" ]] || return 0
  kubectl -n "${AGENT_REGISTRY_NAMESPACE:-agentregistry}" port-forward \
    service/agentregistry "${local_port}:12121" >"${log_file}" 2>&1 &
  agentic_registry_port_forward_pid=$!
  recsys_wait_http "http://127.0.0.1:${local_port}/openapi.json" 30 1 \
    "${agentic_registry_port_forward_pid}"
  arctl configure --url "http://127.0.0.1:${local_port}" >/dev/null
}

agentic_registry_close_tunnel() {
  if [[ -n "${agentic_registry_port_forward_pid}" ]]; then
    recsys_cleanup_process "${agentic_registry_port_forward_pid}"
    agentic_registry_port_forward_pid=""
  fi
}

agentic_registry_git_url() {
  local remote
  remote="${AGENT_REGISTRY_GIT_URL:-$(git config --get remote.origin.url)}"
  [[ -n "${remote}" ]] || {
    recsys_error "Agent Registry publish requires AGENT_REGISTRY_GIT_URL or origin"
    return 2
  }
  printf '%s\n' "${remote}"
}

agentic_assert_registry_publish_branch() {
  local branch="${BRANCH_NAME:-${GIT_BRANCH:-$(git branch --show-current)}}"
  case "${branch}" in
    main|origin/main|refs/heads/main|refs/remotes/origin/main) ;;
    *)
      if ! recsys_is_true "${DEPLOY_PULL_REQUESTS:-0}" \
        && ! recsys_is_true "${FORCE_DEPLOY:-0}" \
        && { ! git rev-parse --verify 'origin/main^{commit}' >/dev/null 2>&1 \
          || [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; }; then
        recsys_error "Agent Registry publication is not authorized on ${branch:-detached}"
        return 2
      fi
      ;;
  esac
  command -v arctl >/dev/null 2>&1 || {
    recsys_error "arctl is required for Agent Registry publication"
    return 2
  }
}
