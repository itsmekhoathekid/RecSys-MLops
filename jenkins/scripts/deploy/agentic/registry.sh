#!/usr/bin/env bash

# Stable compatibility loader. Keep transport, publication, and runtime
# verification independently testable and small.
agentic_registry_module_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${agentic_registry_module_dir}/registry_transport.sh"
source "${agentic_registry_module_dir}/registry_publish.sh"
source "${agentic_registry_module_dir}/registry_verify.sh"
unset agentic_registry_module_dir
