#!/usr/bin/env bash

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
}

agentic_registry_close_tunnel() {
  if [[ -n "${agentic_registry_port_forward_pid}" ]]; then
    recsys_cleanup_process "${agentic_registry_port_forward_pid}"
    agentic_registry_port_forward_pid=""
  fi
}

agentic_registry_version() {
  local commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  printf '0.1.0+%s\n' "${commit:0:12}"
}

agentic_registry_tag() {
  local version="${1:-$(agentic_registry_version)}"
  # Agent Registry v0.4 tags exclude SemVer's build-metadata '+'. Preserve
  # the requested version verbatim in annotations and use a stable safe tag.
  printf '%s\n' "${version/+/-}"
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

agentic_registry_publish_required() {
  local kind="$1"
  local name="$2"
  local tag="$3"
  local version="$4"
  local commit="$5"
  local output
  output="$(mktemp)"
  arctl get "${kind}" "${name}" --tag "${tag}" -o json >"${output}" 2>/dev/null || {
    rm -f "${output}"
    return 0
  }
  if python3 - "${output}" "${version}" "${commit}" <<'PY'
import json
import sys

path, version, commit = sys.argv[1:]
payload = json.load(open(path, encoding="utf-8"))
serialized = json.dumps(payload, sort_keys=True)
raise SystemExit(0 if version in serialized and commit in serialized else 1)
PY
  then
    rm -f "${output}"
    recsys_log DEPLOY "registry ${kind} ${name}@${version} already matches ${commit}"
    return 1
  fi
  rm -f "${output}"
  recsys_error "registry ${kind} ${name}@${version} exists with different metadata"
  return 2
}

agentic_registry_require_dependency() {
  local kind="$1"
  local name="$2"
  local tag="$3"
  local version="$4"
  local commit="$5"
  local output
  output="$(mktemp)"
  if ! arctl get "${kind}" "${name}" --tag "${tag}" -o json >"${output}"; then
    rm -f "${output}"
    recsys_error "matching registry dependency is required: ${kind} ${name}@${tag}"
    return 1
  fi
  if ! python3 - "${output}" "${version}" "${commit}" <<'PY'
import json
import sys

path, version, commit = sys.argv[1:]
payload = json.load(open(path, encoding="utf-8"))
serialized = json.dumps(payload, sort_keys=True)
raise SystemExit(0 if version in serialized and commit in serialized else 1)
PY
  then
    rm -f "${output}"
    recsys_error "registry dependency metadata does not match ${commit}: ${name}@${tag}"
    return 1
  fi
  rm -f "${output}"
}

agentic_registry_tagged_resource_exists() {
  local kind="$1"
  local name="$2"
  local output="$3"
  arctl get "${kind}" "${name}" --all-tags -o json >"${output}" 2>/dev/null \
    || return 1
  python3 - "${output}" <<'PY' >/dev/null 2>&1
import json
import sys

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except (json.JSONDecodeError, OSError):
    raise SystemExit(1)
raise SystemExit(0 if isinstance(payload, list) and payload else 1)
PY
}

agentic_write_registry_manifest() {
  local path="$1"
  local kind="$2"
  local qualified_name="$3"
  local version="$4"
  local tag="$5"
  local commit="$6"
  local git_url="$7"
  python3 - "${path}" "${kind}" "${qualified_name}" "${version}" \
    "${tag}" "${commit}" "${git_url}" <<'PY'
import json
import sys

path, kind, qualified_name, version, tag, commit, git_url = sys.argv[1:]
namespace, name = qualified_name.split("/", 1)
metadata = {
    "namespace": namespace,
    "name": name,
    "tag": tag,
    "labels": {
        "app.kubernetes.io/part-of": "recsys-agentic-context",
        "recsys.dev/git-sha": commit[:12],
    },
    "annotations": {
        "recsys.dev/version": version,
        "recsys.dev/git-commit": commit,
        "recsys.dev/source": git_url,
    },
}
if kind == "mcp":
    resource = {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "MCPServer",
        "metadata": metadata,
        "spec": {
            "title": "RecSys Feature/RAG MCP",
            "description": "Grounded online-feature, exact-chunk and semantic RAG tools",
            "remote": {
                "type": "streamable-http",
                "url": "http://recsys-feature-rag-mcp.kagent.svc.cluster.local:8080/mcp",
            },
        },
    }
elif kind == "agent":
    variant = "sandbox" if name.endswith("-sandbox") else "regular"
    metadata["labels"]["recsys.dev/variant"] = variant
    resource = {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "Agent",
        "metadata": metadata,
        "spec": {
            "title": f"RecSys Context Agent ({variant})",
            "description": "Grounded personalization, exact chunk and semantic RAG agent",
            "mcpServers": [{
                "kind": "MCPServer",
                "namespace": namespace,
                "name": "recsys-feature-rag-mcp",
                "tag": tag,
            }],
        },
    }
elif kind == "recommendation-mcp":
    metadata["labels"]["app.kubernetes.io/part-of"] = "recsys-recommendation-agentic"
    resource = {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "MCPServer",
        "metadata": metadata,
        "spec": {
            "title": "RecSys Recommendation MCP",
            "description": "Recommendation-only facade for recsys-inference-api",
            "remote": {
                "type": "streamable-http",
                "url": "http://recsys-recommendation-mcp.kagent.svc.cluster.local:8080/mcp",
            },
        },
    }
elif kind == "recommendation-agent":
    metadata["labels"].update({
        "app.kubernetes.io/part-of": "recsys-recommendation-agentic",
        "recsys.dev/variant": "sandbox",
    })
    resource = {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "Agent",
        "metadata": metadata,
        "spec": {
            "title": "RecSys Recommendation Agent (sandbox)",
            "description": "gVisor agent that presents inference rankings without reranking",
            "mcpServers": [{
                "kind": "MCPServer", "namespace": namespace,
                "name": "recsys-recommendation-mcp", "tag": tag,
            }],
        },
    }
elif kind == "coordinator-agent":
    metadata["labels"].update({
        "app.kubernetes.io/part-of": "recsys-coordinator-agentic",
        "recsys.dev/variant": "sandbox",
    })
    metadata["annotations"]["recsys.dev/a2a-dependencies"] = ",".join([
        f"recsys/recsys-context-agent-sandbox@{tag}",
        f"recsys/recsys-recommendation-agent-sandbox@{tag}",
    ])
    resource = {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "Agent",
        "metadata": metadata,
        "spec": {
            "title": "RecSys Coordinator Agent (sandbox)",
            "description": (
                "Intent-routing coordinator for context, RAG, and "
                "recommendation specialists"
            ),
            "mcpServers": [
                {
                    "kind": "MCPServer",
                    "namespace": namespace,
                    "name": "recsys-feature-rag-mcp",
                    "tag": tag,
                },
                {
                    "kind": "MCPServer",
                    "namespace": namespace,
                    "name": "recsys-recommendation-mcp",
                    "tag": tag,
                },
            ],
        },
    }
else:
    raise SystemExit(f"unsupported registry resource kind: {kind}")
with open(path, "w", encoding="utf-8") as stream:
    json.dump(resource, stream, indent=2, sort_keys=True)
PY
}

agentic_assert_registry_publish_branch() {
  local branch="${BRANCH_NAME:-${GIT_BRANCH:-$(git branch --show-current)}}"
  case "${branch}" in
    main|origin/main|refs/heads/main|refs/remotes/origin/main) ;;
    *)
      if ! recsys_is_true "${DEPLOY_PULL_REQUESTS:-0}" \
        && ! recsys_is_true "${FORCE_DEPLOY:-0}" \
        && { ! git rev-parse --verify origin/main^{commit} >/dev/null 2>&1 \
          || [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; }; then
        recsys_log DEPLOY "skip Agent Registry publish on non-main branch ${branch:-detached}"
        return 1
      fi
      ;;
  esac
  command -v arctl >/dev/null 2>&1 || {
    recsys_error "arctl is required for Agent Registry publish"
    return 2
  }
}

agentic_write_registry_evidence() {
  recsys_write_registry_evidence "$@"
}

agentic_wait_for_regular_agent_removal() {
  local resource
  for resource in \
    agent/recsys-context-agent \
    deployment/recsys-context-agent \
    scaledobject/recsys-context-agent \
    hpa/keda-hpa-recsys-context-agent; do
    for _ in $(seq 1 120); do
      if ! kubectl -n kagent get "${resource}" >/dev/null 2>&1; then
        break
      fi
      sleep 2
    done
    if kubectl -n kagent get "${resource}" >/dev/null 2>&1; then
      recsys_error "legacy regular Agent resource still exists: ${resource}"
      return 1
    fi
  done
}

agentic_write_context_registry_evidence() {
  local path="$1"
  local version="$2"
  local commit="$3"
  local active_artifact="$4"
  local backup_path="$5"
  local removed="$6"
  python3 - "${path}" "${version}" "${commit}" "${active_artifact}" \
    "${backup_path}" "${removed}" <<'PY'
import json
import sys

path, version, commit, active, backup, removed = sys.argv[1:]
payload = {
    "version": version,
    "git_commit": commit,
    "artifacts": [active],
    "removed_artifacts": (["recsys/recsys-context-agent@all-tags"] if removed == "true" else []),
    "legacy_registry_backup": backup,
}
with open(path, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, indent=2, sort_keys=True)
PY
}

publish_feature_rag_mcp_registry() {
  local version tag commit git_url registry_name manifest
  agentic_assert_registry_publish_branch || return 0
  agentic_preflight true
  agentic_mcp_protocol_smoke
  agentic_registry_open_tunnel
  commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  version="$(agentic_registry_version)"
  tag="$(agentic_registry_tag "${version}")"
  git_url="$(agentic_registry_git_url)"
  registry_name="${AGENT_REGISTRY_MCP_NAME:-recsys/recsys-feature-rag-mcp}"
  manifest="$(mktemp)"
  agentic_write_registry_manifest "${manifest}" mcp "${registry_name}" \
    "${version}" "${tag}" "${commit}" "${git_url}"
  if agentic_registry_publish_required mcp "${registry_name}" "${tag}" \
    "${version}" "${commit}"; then
    arctl apply -f "${manifest}"
  else
    [[ "$?" == "1" ]]
  fi
  rm -f "${manifest}"
  arctl get mcp "${registry_name}" --tag "${tag}" -o json >/dev/null
  agentic_write_registry_evidence \
    .ci-deploy/feature-rag-mcp-registry.json "${version}" "${commit}" \
    "${registry_name}@${tag}"
}

publish_context_agent_registry() {
  local version tag commit git_url registry_name manifest
  local legacy_name legacy_backup legacy_check legacy_present=false
  agentic_assert_registry_publish_branch || return 0
  agentic_preflight true
  kubectl -n kagent wait --for=condition=Ready \
    sandboxagent/recsys-context-agent-sandbox --timeout="${timeout}"
  kubectl -n kagent rollout status \
    deployment/recsys-context-sandbox-pool --timeout="${timeout}"
  agentic_wait_for_regular_agent_removal
  agentic_a2a_smoke recsys-context-agent-sandbox
  agentic_registry_open_tunnel
  commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  version="$(agentic_registry_version)"
  tag="$(agentic_registry_tag "${version}")"
  git_url="$(agentic_registry_git_url)"
  registry_name="recsys/recsys-context-agent-sandbox"
  manifest="$(mktemp)"
  agentic_write_registry_manifest "${manifest}" agent "${registry_name}" \
    "${version}" "${tag}" "${commit}" "${git_url}"
  if agentic_registry_publish_required agent "${registry_name}" "${tag}" \
    "${version}" "${commit}"; then
    arctl apply -f "${manifest}"
  else
    [[ "$?" == "1" ]]
  fi
  rm -f "${manifest}"
  arctl get agent "${registry_name}" --tag "${tag}" -o json >/dev/null

  mkdir -p .ci-deploy
  legacy_name="recsys/recsys-context-agent"
  legacy_backup=".ci-deploy/recsys-context-agent-registry-backup.json"
  if agentic_registry_tagged_resource_exists agent "${legacy_name}" \
    "${legacy_backup}"; then
    legacy_present=true
    arctl delete agent "${legacy_name}" --all-tags
  else
    python3 - "${legacy_backup}" <<'PY'
import json
import sys

with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump([], stream, indent=2, sort_keys=True)
PY
  fi
  legacy_check="$(mktemp)"
  if agentic_registry_tagged_resource_exists agent "${legacy_name}" \
    "${legacy_check}"; then
    rm -f "${legacy_check}"
    recsys_error "legacy regular Agent still exists in Agent Registry"
    return 1
  fi
  rm -f "${legacy_check}"
  agentic_write_context_registry_evidence \
    .ci-deploy/context-agent-registry.json "${version}" "${commit}" \
    "${registry_name}@${tag}" "${legacy_backup}" "${legacy_present}"
}

publish_recommendation_mcp_registry() {
  local version tag commit git_url registry_name manifest
  agentic_assert_registry_publish_branch || return 0
  recommendation_agentic_preflight true
  recommendation_mcp_protocol_smoke
  agentic_registry_open_tunnel
  commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  version="$(agentic_registry_version)"
  tag="$(agentic_registry_tag "${version}")"
  git_url="$(agentic_registry_git_url)"
  registry_name="recsys/recsys-recommendation-mcp"
  manifest="$(mktemp)"
  agentic_write_registry_manifest "${manifest}" recommendation-mcp \
    "${registry_name}" "${version}" "${tag}" "${commit}" "${git_url}"
  if agentic_registry_publish_required mcp "${registry_name}" "${tag}" \
    "${version}" "${commit}"; then
    arctl apply -f "${manifest}"
  else
    [[ "$?" == "1" ]]
  fi
  rm -f "${manifest}"
  arctl get mcp "${registry_name}" --tag "${tag}" -o json >/dev/null
  agentic_write_registry_evidence \
    .ci-deploy/recommendation-mcp-registry.json "${version}" "${commit}" \
    "${registry_name}@${tag}"
}

publish_recommendation_agent_registry() {
  local version tag commit git_url registry_name manifest
  agentic_assert_registry_publish_branch || return 0
  recommendation_agentic_preflight true
  kubectl -n kagent wait --for=condition=Ready \
    sandboxagent/recsys-recommendation-agent-sandbox --timeout="${timeout}"
  kubectl -n kagent rollout status \
    deployment/recsys-recommendation-sandbox-pool --timeout="${timeout}"
  recommendation_a2a_smoke
  agentic_registry_open_tunnel
  commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  version="$(agentic_registry_version)"
  tag="$(agentic_registry_tag "${version}")"
  git_url="$(agentic_registry_git_url)"
  registry_name="recsys/recsys-recommendation-agent-sandbox"
  arctl get mcp recsys/recsys-recommendation-mcp \
    --tag "${tag}" -o json >/dev/null || {
    recsys_error "matching recommendation MCP registry version is required"
    return 1
  }
  manifest="$(mktemp)"
  agentic_write_registry_manifest "${manifest}" recommendation-agent \
    "${registry_name}" "${version}" "${tag}" "${commit}" "${git_url}"
  if agentic_registry_publish_required agent "${registry_name}" "${tag}" \
    "${version}" "${commit}"; then
    arctl apply -f "${manifest}"
  else
    [[ "$?" == "1" ]]
  fi
  rm -f "${manifest}"
  arctl get agent "${registry_name}" --tag "${tag}" -o json >/dev/null
  agentic_write_registry_evidence \
    .ci-deploy/recommendation-agent-registry.json "${version}" "${commit}" \
    "${registry_name}@${tag}"
}

publish_coordinator_agent_registry() {
  local version tag commit git_url registry_name manifest legacy_name legacy_backup
  agentic_assert_registry_publish_branch || return 0
  coordinator_agentic_preflight true
  agentic_mcp_protocol_smoke
  recommendation_mcp_protocol_smoke
  coordinator_a2a_smoke
  agentic_registry_open_tunnel
  commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  version="$(agentic_registry_version)"
  tag="$(agentic_registry_tag "${version}")"
  git_url="$(agentic_registry_git_url)"
  registry_name="recsys/recsys-coordinator-agent-sandbox"
  agentic_registry_require_dependency mcp \
    recsys/recsys-feature-rag-mcp "${tag}" "${version}" "${commit}"
  agentic_registry_require_dependency mcp \
    recsys/recsys-recommendation-mcp "${tag}" "${version}" "${commit}"
  agentic_registry_require_dependency agent \
    recsys/recsys-context-agent-sandbox "${tag}" "${version}" "${commit}"
  agentic_registry_require_dependency agent \
    recsys/recsys-recommendation-agent-sandbox "${tag}" "${version}" "${commit}"
  manifest="$(mktemp)"
  agentic_write_registry_manifest "${manifest}" coordinator-agent \
    "${registry_name}" "${version}" "${tag}" "${commit}" "${git_url}"
  if agentic_registry_publish_required agent "${registry_name}" "${tag}" \
    "${version}" "${commit}"; then
    arctl apply -f "${manifest}"
  else
    [[ "$?" == "1" ]]
  fi
  rm -f "${manifest}"
  arctl get agent "${registry_name}" --tag "${tag}" -o json >/dev/null
  legacy_name="recsys/recsys-coordinator-agent"
  legacy_backup=".ci-deploy/recsys-coordinator-agent-registry-backup.json"
  if agentic_registry_tagged_resource_exists agent "${legacy_name}" \
    "${legacy_backup}"; then
    arctl delete agent "${legacy_name}" --all-tags
  else
    printf '[]\n' >"${legacy_backup}"
  fi
  agentic_write_registry_evidence \
    .ci-deploy/coordinator-agent-registry.json "${version}" "${commit}" \
    "${registry_name}@${tag}" \
    "recsys/recsys-context-agent-sandbox@${tag}" \
    "recsys/recsys-recommendation-agent-sandbox@${tag}" \
    "recsys/recsys-feature-rag-mcp@${tag}" \
    "recsys/recsys-recommendation-mcp@${tag}"
}
