#!/usr/bin/env bash
set -euo pipefail

component="${1:-all}"

case "${component}" in
  data-generator)
    PYTHONPATH=apps/data-platform/data-generator/src uv run pytest tests/unit/data_generator -q
    ;;
  data-platform)
    PYTHONPATH=apps/data-platform/src:apps/data-platform/data-generator/src uv run pytest tests/unit/data_platform tests/contract -q
    ;;
  feature-store)
    PYTHONPATH=apps/data-platform/src:apps/data-platform/feature-store/src \
      uv run python -c 'from pathlib import Path; assert Path("apps/data-platform/feature-store/feature_repo/feature_store.yaml").exists(); import validate_feature_store; import feature_store.feast_registry'
    ;;
  ml-system)
    PYTHONPATH=apps/ml-system/src:apps/data-platform/src uv run pytest tests/unit/ml_system -q
    ;;
  kfp)
    PYTHONPATH=apps/ml-system/src:apps/data-platform/src uv run python apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py
    ;;
  all)
    "$0" data-generator
    "$0" data-platform
    "$0" feature-store
    "$0" ml-system
    "$0" kfp
    ;;
  *)
    echo "Unknown component: ${component}" >&2
    exit 2
    ;;
esac

