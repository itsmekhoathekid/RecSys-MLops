pipeline {
  agent any
  options {
    skipDefaultCheckout(true)
    disableConcurrentBuilds()
    durabilityHint('MAX_SURVIVABILITY')
    lock(resource: 'recsys-production-release')
    timeout(time: 25, unit: 'MINUTES')
  }
  parameters {
    string(name: 'IMAGE', defaultValue: '', description: 'Operator-owned evaluator/router image digest. Does not start an experiment.')
  }
  environment {
    AB_PREPARATION_IMAGE = "${params.IMAGE}"
    AB_SOURCE_CHECKSUM = '__SOURCE_CHECKSUM__'
  }
  stages {
    stage('Load Reviewed Preparation Bundle') {
      steps {
        script {
          writeFile file: '.llm-agent-cd/evaluation-preparation.json', text: groovy.json.JsonOutput.toJson([
            stage: 'NOT_STARTED', build_url: env.BUILD_URL, build_number: env.BUILD_NUMBER, accepted: false
          ])
        }
        writeFile file: 'workflow-preparation.tgz', text: __BUNDLE__, encoding: 'Base64'
        sh '''
          set -eu
          printf '%s  workflow-preparation.tgz\n' "$AB_SOURCE_CHECKSUM" | sha256sum -c -
          tar -xzf workflow-preparation.tgz
          python3 -m venv .workflow-preparation-venv
          .workflow-preparation-venv/bin/pip install -r apps/agentic/llm_ab_router/requirements.txt
        '''
      }
    }
    stage('Capacity, Additive Schema and Suspended Evaluation') {
      steps {
        withCredentials([file(credentialsId: 'recsys-workflow-ab-env', variable: 'AB_ENV_FILE')]) {
          lock(resource: 'recsys-workflow-state') {
            sh '.workflow-preparation-venv/bin/python -m jenkins.python.llm_agent_cd.evaluation_deploy --image "$AB_PREPARATION_IMAGE"'
          }
        }
      }
    }
  }
  post {
    always {
      script {
        if (currentBuild.currentResult != 'SUCCESS') {
          writeFile file: '.llm-agent-cd/evaluation-preparation.json', text: groovy.json.JsonOutput.toJson([
            stage: 'PREPARATION_FAILED', build_url: env.BUILD_URL, build_number: env.BUILD_NUMBER,
            accepted: false, result: currentBuild.currentResult,
            evidence: 'See this build log and create-only MinIO probe records; never reuse an earlier build artifact.'
          ])
        }
      }
      archiveArtifacts allowEmptyArchive: true, artifacts: '.llm-agent-cd/evaluation-preparation.json'
    }
  }
}
