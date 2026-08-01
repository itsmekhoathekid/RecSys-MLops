#!/usr/bin/env bash

ci_analytics() {
  run_plain_pytest "analytics" "apps/analytics/src:apps/data-platform/src" \
    tests/unit/analytics tests/contract/test_analytics_contracts.py
  helm lint infra/helm/recsys-analytics
  helm template recsys-analytics infra/helm/recsys-analytics >/dev/null
}
