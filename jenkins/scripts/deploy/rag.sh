#!/usr/bin/env bash

# Public compatibility loader for RAG deployment helpers. Index creation and
# pointer promotion remain available for Airflow-owned workflows; Jenkins only
# dispatches bootstrap, deployment readiness, and active-index verification.
rag_deploy_module_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/rag" && pwd)"
source jenkins/scripts/lib/runtime.sh
source "${rag_deploy_module_dir}/kubernetes.sh"
source "${rag_deploy_module_dir}/bootstrap.sh"
source "${rag_deploy_module_dir}/api.sh"
source "${rag_deploy_module_dir}/index_lifecycle.sh"
source "${rag_deploy_module_dir}/rollback.sh"
unset rag_deploy_module_dir
