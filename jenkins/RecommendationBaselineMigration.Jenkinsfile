pipeline {
  agent any
  options {
    skipDefaultCheckout(true)
    disableConcurrentBuilds()
    durabilityHint('MAX_SURVIVABILITY')
    timeout(time: 30, unit: 'MINUTES')
  }
  parameters {
    string(name: 'ROUTER_IMAGE', defaultValue: '', description: 'Digest-pinned deployed Recommendation router image.')
  }
  environment {
    AB_STATE_URI = 's3://recsys-llm-ab/recommendation/state.json'
  }
  stages {
    stage('Load immutable source') {
      steps {
        sh '''
          set -eu
          installed_image="$(kubectl -n kagent get deploy recsys-ab-router -o jsonpath='{.spec.template.spec.containers[0].image}')"
          requested_image="${ROUTER_IMAGE:-$installed_image}"
          test "$installed_image" = "$requested_image"
          kubectl -n kagent exec deployment/recsys-ab-router -- tar -C /app -cf - jenkins apps configs | tar -xf -
          python3 -m venv .llm-ab-venv
          .llm-ab-venv/bin/pip install -r apps/agentic/llm_ab_router/requirements.txt
        '''
      }
    }
    stage('Migrate verified Recommendation baseline') {
      steps {
        withCredentials([file(credentialsId: 'recsys-llm-ab-env', variable: 'AB_ENV_FILE')]) {
          lock(resource: 'recsys-production-release') {
            sh '''
              set -eu
              installed_image="$(kubectl -n kagent get deploy recsys-ab-router -o jsonpath='{.spec.template.spec.containers[0].image}')"
              requested_image="${ROUTER_IMAGE:-$installed_image}"
              test "$installed_image" = "$requested_image"
              .llm-ab-venv/bin/python -m jenkins.python.llm_agent_cd.recommendation_baseline_migration --image "$requested_image"
            '''
          }
        }
      }
    }
  }
}
