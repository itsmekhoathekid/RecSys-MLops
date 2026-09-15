#!/usr/bin/env bash
# The public entrypoint only validates and dispatches. Helm mutation is allowed
# exclusively inside RecSys-Global-Model-Config while it holds the shared
# recsys-production-release Jenkins lock.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python -m jenkins.python.llm_agent_cd.global_config_dispatch "$@"
fi
if command -v uv >/dev/null 2>&1; then
  exec uv run python -m jenkins.python.llm_agent_cd.global_config_dispatch "$@"
fi
exec python3 -m jenkins.python.llm_agent_cd.global_config_dispatch "$@"
