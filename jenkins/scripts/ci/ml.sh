#!/usr/bin/env bash

ci_training() {
  tests=(tests/unit/ml_system)
  append_integration_dir training
  cov_paths=(kubeflow.components.runtime kubeflow.pipelines.bst_training_pipeline kubeflow.pipelines.compile_training_pipeline)
  component_pytest "${component}" "apps/ml-system/src:apps/data-platform/src"
  run_kfp_compile
}

ci_rollout() {
  tests=(tests/unit/ml_system/test_model_rollout_controller.py tests/contract/test_serving_contracts.py)
  append_integration_dir rollout
  cov_paths=(model_cd)
  component_pytest "${component}" "jenkins/scripts:apps/ml-system/src:apps/data-platform/src"
  helm lint infra/helm/recsys-ci
  helm template recsys-ci infra/helm/recsys-ci \
    --set modelRolloutWatcher.enabled=true \
    --set modelRolloutWatcher.image=registry.example/recsys-mlops-training:ci >/dev/null
  helm lint infra/helm/recsys-serving
  bash -n jenkins/scripts/test/champion_only.sh
}
