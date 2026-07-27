#!/usr/bin/env bash

ci_demo_web() {
  local demo_backend_env="${PWD}/apps/demo-web/backend/.venv"
  local frontend_container=""
  PYTHONPATH=apps/demo-web/backend UV_PROJECT_ENVIRONMENT="${demo_backend_env}" \
    uv run --project apps/demo-web/backend ruff check apps/demo-web/backend/app apps/demo-web/backend/tests
  PYTHONPATH=apps/demo-web/backend UV_PROJECT_ENVIRONMENT="${demo_backend_env}" \
    uv run --project apps/demo-web/backend ruff format --check apps/demo-web/backend/app apps/demo-web/backend/tests
  UV_PROJECT_ENVIRONMENT="${demo_backend_env}" uv run --project apps/demo-web/backend pip-audit
  PYTHONPATH=apps/demo-web/backend UV_PROJECT_ENVIRONMENT="${demo_backend_env}" \
    uv run --project apps/demo-web/backend pytest \
    apps/demo-web/backend/tests tests/contract/test_demo_web_contracts.py -q \
    --cov=apps/demo-web/backend/app \
    --cov-report="xml:${reports_dir}/coverage/demo_web_backend.xml" \
    --cov-fail-under="${coverage_min}" \
    --junitxml="${reports_dir}/junit/demo_web_backend.xml"

  if command -v node >/dev/null 2>&1 && [[ "$(node -p 'process.versions.node.split(`.`)[0]')" -ge 22 ]]; then
    run_demo_frontend() {
      (cd apps/demo-web/frontend && "$@")
    }
    copy_demo_frontend_coverage() {
      cp -R apps/demo-web/frontend/coverage/. "${reports_dir}/coverage/demo_web_frontend/"
    }
  else
    frontend_container="recsys-demo-web-ci-${BUILD_NUMBER:-$$}"
    frontend_container="${frontend_container//[^a-zA-Z0-9_.-]/-}"
    docker rm -f "${frontend_container}" >/dev/null 2>&1 || true
    docker create --name "${frontend_container}" -w /workspace \
      node:24-bookworm-slim sleep infinity >/dev/null
    docker start "${frontend_container}" >/dev/null
    docker exec "${frontend_container}" mkdir -p \
      /workspace/apps/demo-web/frontend /workspace/apps/demo-web/backend
    docker cp apps/demo-web/frontend/. "${frontend_container}:/workspace/apps/demo-web/frontend"
    docker cp apps/demo-web/backend/openapi.json "${frontend_container}:/workspace/apps/demo-web/backend/openapi.json"
    cleanup_demo_frontend() {
      docker rm -f "${frontend_container}" >/dev/null 2>&1 || true
    }
    trap cleanup_demo_frontend EXIT
    run_demo_frontend() {
      docker exec -e HOME=/tmp -w /workspace/apps/demo-web/frontend "${frontend_container}" "$@"
    }
    copy_demo_frontend_coverage() {
      docker cp "${frontend_container}:/workspace/apps/demo-web/frontend/coverage/." \
        "${reports_dir}/coverage/demo_web_frontend/"
    }
  fi
  run_demo_frontend npm ci
  run_demo_frontend npm audit --audit-level=high
  run_demo_frontend npm run lint
  run_demo_frontend npm run format:check
  run_demo_frontend npm run typecheck
  run_demo_frontend npm test
  run_demo_frontend npm run build
  mkdir -p "${reports_dir}/coverage/demo_web_frontend"
  copy_demo_frontend_coverage
  helm lint infra/helm/recsys-demo-web -f infra/helm/recsys-demo-web/values-gcp.yaml
  helm template recsys-demo-web infra/helm/recsys-demo-web \
    -f infra/helm/recsys-demo-web/values-gcp.yaml --namespace api-serving >/dev/null
}
