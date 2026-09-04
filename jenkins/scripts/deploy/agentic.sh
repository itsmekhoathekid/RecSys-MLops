#!/usr/bin/env bash

# Public compatibility loader for agent deployment helpers. Keep this path
# stable: Jenkins and existing automation source it directly.
agentic_deploy_module_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/agentic" && pwd)"
source jenkins/scripts/lib/runtime.sh
source "${agentic_deploy_module_dir}/sandbox.sh"
source "${agentic_deploy_module_dir}/kubernetes.sh"
source "${agentic_deploy_module_dir}/mcp.sh"
source "${agentic_deploy_module_dir}/a2a.sh"
source "${agentic_deploy_module_dir}/registry.sh"
unset agentic_deploy_module_dir
