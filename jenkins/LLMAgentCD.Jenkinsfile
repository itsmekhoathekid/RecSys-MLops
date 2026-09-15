pipeline {
  agent any
  options {
    skipDefaultCheckout(true)
    disableConcurrentBuilds()
    durabilityHint('MAX_SURVIVABILITY')
    timeout(time: 2, unit: 'HOURS')
  }
  parameters {
    choice(name: 'SOURCE_MODE', choices: ['scm', 'deployed-image'], description: 'Use the digest-pinned production image when source is not merged yet.')
    choice(name: 'ACTION', choices: ['prepare', 'route', 'promote', 'rollback', 'cleanup'], description: 'One controller-authorized action per build.')
    string(name: 'ACTION_KEY', defaultValue: '', description: 'Immutable controller action identity.')
    string(name: 'EXPERIMENT_ID', defaultValue: '', description: 'Recommendation experiment ID.')
    string(name: 'EXPECTED_PHASE', defaultValue: '', description: 'Phase captured by the controller.')
    string(name: 'EXPECTED_STATE_ETAG', defaultValue: '', description: 'State ETag captured by the controller.')
    choice(name: 'TARGET_WEIGHT', choices: ['', '0', '10', '50', '100'], description: 'Required only for route.')
    choice(name: 'EXPERIMENT_TYPE', choices: ['llm_only', 'config_only', 'combined'], description: 'Prepare-only experiment axis.')
    string(name: 'BASELINE_RELEASE_ID', defaultValue: '', description: 'Expected immutable champion for prepare.')
    string(name: 'CANDIDATE_MANIFEST', defaultValue: '', description: 'Prepare-only immutable candidate URI.')
    string(name: 'STATE_URI', defaultValue: 's3://recsys-llm-ab/recommendation/state.json', description: 'Recommendation CAS state object.')
    string(name: 'ROUTER_IMAGE', defaultValue: '', description: 'Digest-pinned Recommendation A/B image.')
    string(name: 'POLICY', defaultValue: 'configs/llm-ab/recommendation-live-test-policy.json', description: 'Frozen gate policy.')
    string(name: 'FIXTURES', defaultValue: 'configs/llm-ab/cases.json', description: 'Exactly 20 fixture cases.')
    string(name: 'REASON', defaultValue: '', description: 'Controller rollback reason.')
  }
  environment {
    AB_STATE_URI = "${params.STATE_URI}"
    AB_ROUTER_IMAGE = "${params.ROUTER_IMAGE}"
    AB_SCOPE = 'recommendation'
  }
  stages {
    stage('Checkout and Validate Runtime') {
      steps {
        script {
          if (params.SOURCE_MODE == 'deployed-image') {
            sh '''
              set -eu
              installed_image="$(kubectl -n kagent get deployment recsys-ab-router -o jsonpath='{.spec.template.spec.containers[0].image}')"
              test "$installed_image" = "$AB_ROUTER_IMAGE"
              kubectl -n kagent exec deployment/recsys-ab-router -- tar -C /app -cf - jenkins apps configs | tar -xf -
            '''
          } else {
            checkout scm
          }
        }
        sh '''
          set -eu
          command -v kubectl
          python3 -m venv .llm-ab-venv
          .llm-ab-venv/bin/pip install -q -r apps/agentic/llm_ab_router/requirements.txt
          .llm-ab-venv/bin/python -m compileall -q jenkins/python/llm_agent_cd apps/agentic/llm_ab_router
        '''
      }
    }
    stage('Execute Controller Action') {
      steps {
        withCredentials([file(credentialsId: 'recsys-llm-ab-env', variable: 'AB_ENV_FILE')]) {
          lock(resource: 'recsys-production-release') {
            sh '''
              set -eu
              set -- "$ACTION" \
                --action-key "$ACTION_KEY" \
                --experiment-id "$EXPERIMENT_ID" \
                --expected-phase "$EXPECTED_PHASE" \
                --expected-state-etag "$EXPECTED_STATE_ETAG" \
                --state-uri "$STATE_URI" \
                --policy "$POLICY" \
                --fixtures "$FIXTURES"
              [ -z "${TARGET_WEIGHT:-}" ] || set -- "$@" --target-weight "$TARGET_WEIGHT"
              [ -z "${CANDIDATE_MANIFEST:-}" ] || set -- "$@" --candidate "$CANDIDATE_MANIFEST"
              [ -z "${EXPERIMENT_TYPE:-}" ] || set -- "$@" --mode "$EXPERIMENT_TYPE"
              [ -z "${REASON:-}" ] || set -- "$@" --reason "$REASON"
              .llm-ab-venv/bin/python -m jenkins.python.llm_agent_cd.recommendation_action "$@"
            '''
          }
        }
      }
    }
  }
  post {
    always {
      archiveArtifacts allowEmptyArchive: true, artifacts: '.llm-agent-cd/**/*.json'
    }
    unsuccessful {
      echo 'The controller will decide rollback or NEEDS_ATTENTION from durable action evidence.'
    }
  }
}
