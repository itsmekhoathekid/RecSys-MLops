pipeline {
  agent any

  options {
    skipDefaultCheckout(true)
    disableConcurrentBuilds()
    timestamps()
  }

  parameters {
    choice(name: 'SERVICE', choices: ['all', 'inference', 'online-feature', 'rag'], description: 'Serving mutation scope. Nightly runs should keep all.')
    string(name: 'MAX_CHILDREN', defaultValue: '4', description: 'Maximum parallel Mutmut workers.')
  }

  triggers {
    cron('H H * * *')
  }

  environment {
    UV_LINK_MODE = 'copy'
    RECSYS_OTEL_ENABLED = '0'
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
      }
    }

    stage('Mutation gate') {
      steps {
        sh '''
          set -euo pipefail
          case "${MAX_CHILDREN}" in
            ''|*[!0-9]*) echo "MAX_CHILDREN must be a positive integer" >&2; exit 2 ;;
          esac
          test "${MAX_CHILDREN}" -ge 1
          mkdir -p tests/mutation/api_serving/reports
          uv run --frozen python tests/mutation/api_serving/run.py \
            "${SERVICE}" \
            --max-children "${MAX_CHILDREN}"
        '''
      }
    }
  }

  post {
    always {
      archiveArtifacts allowEmptyArchive: true, artifacts: 'tests/mutation/api_serving/reports/*.json,tests/mutation/api_serving/reports/*.txt'
    }
  }
}
