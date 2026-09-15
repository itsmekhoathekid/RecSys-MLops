pipeline {
  agent any
  options {
    skipDefaultCheckout(true)
    disableConcurrentBuilds()
    durabilityHint('MAX_SURVIVABILITY')
    timeout(time: 2, unit: 'HOURS')
  }
  parameters {
    choice(name: 'ACTION', choices: ['prepare', 'cleanup'], description: 'Prepare at 0% traffic or retire an expired prepared candidate.')
    string(name: 'ONBOARDING_ID', defaultValue: '', description: 'Deterministic onb- identity.')
    string(name: 'INTENT_URI', defaultValue: '', description: 'Create-only onboarding intent in MinIO.')
    string(name: 'ROUTER_IMAGE', defaultValue: '', description: 'Digest-pinned deployed Recommendation router image.')
  }
  environment {
    AB_ROUTER_IMAGE = "${params.ROUTER_IMAGE}"
    AB_SCOPE = 'recommendation'
    AB_STATE_URI = 's3://recsys-llm-ab/recommendation/state.json'
  }
  stages {
    stage('Validate runtime') {
      steps {
        sh '''
          set -eu
          test -n "$ONBOARDING_ID"
          test -n "$INTENT_URI"
          installed_image="$(kubectl -n kagent get deployment recsys-ab-router -o jsonpath='{.spec.template.spec.containers[0].image}')"
          test "$installed_image" = "$AB_ROUTER_IMAGE"
          kubectl -n kagent exec deployment/recsys-ab-router -- tar -C /app -cf - jenkins apps configs | tar -xf -
          python3 -m venv .llm-onboarding-venv
          .llm-onboarding-venv/bin/pip install -r apps/agentic/llm_ab_router/requirements.txt
          .llm-onboarding-venv/bin/python -m compileall -q jenkins/python/llm_agent_cd apps/agentic/llm_ab_router
        '''
      }
    }
    stage('Attest, prepare and publish') {
      steps {
        withCredentials([file(credentialsId: 'recsys-llm-ab-env', variable: 'AB_ENV_FILE')]) {
          lock(resource: 'recsys-production-release') {
            sh '''
              set -eu
              .llm-onboarding-venv/bin/python -m jenkins.python.llm_agent_cd.onboarding \
                "$ACTION" --intent-uri "$INTENT_URI"
            '''
          }
        }
      }
    }
  }
  post {
    always {
      archiveArtifacts allowEmptyArchive: true, artifacts: '.llm-onboarding/**/*.json'
    }
    unsuccessful {
      echo 'The candidate remains at 0% traffic. Inspect onboarding DB state and compatibility Job logs.'
    }
  }
}
