pipeline {
  agent any

  parameters {
    string(name: 'PROMOTION_MANIFEST_URI', defaultValue: 's3://recsys-model-store/promotions/bst/latest.json', description: 'Versioned or latest promotion manifest produced by Kubeflow model promotion.')
    string(name: 'COMPONENT_DEPLOY_TIMEOUT', defaultValue: '600s', description: 'Timeout for Helm/KServe rolling update checks.')
    string(name: 'MODEL_VERSION', defaultValue: '', description: 'Optional model version label passed by the Kubeflow trigger.')
    string(name: 'METRIC_NAME', defaultValue: '', description: 'Optional promotion metric label passed by the Kubeflow trigger.')
    string(name: 'METRIC_VALUE', defaultValue: '', description: 'Optional promotion metric value passed by the Kubeflow trigger.')
    string(name: 'TRIGGER_SOURCE', defaultValue: 'manual', description: 'Caller that triggered this model deployment.')
  }

  environment {
    UV_LINK_MODE = 'copy'
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
      }
    }

    stage('Python Env') {
      steps {
        script {
          env.CI_TMP_ROOT = "/tmp/recsys-ci-${env.JOB_BASE_NAME}-${env.BUILD_NUMBER}"
          env.UV_PROJECT_ENVIRONMENT = "${env.CI_TMP_ROOT}/venv"
          env.UV_CACHE_DIR = "${env.CI_TMP_ROOT}/uv-cache"
        }
        sh '''
          set -euo pipefail
          mkdir -p "${CI_TMP_ROOT}" "${UV_CACHE_DIR}"
          uv sync
        '''
      }
    }

    stage('KServe Model CD') {
      steps {
        echo "Deploying promoted model ${params.MODEL_VERSION ?: '(version from manifest)'} from ${params.PROMOTION_MANIFEST_URI}"
        echo "Promotion gate: ${params.METRIC_NAME ?: 'metric'}=${params.METRIC_VALUE ?: 'n/a'} from ${params.TRIGGER_SOURCE}"
        sh '''
          set -euo pipefail
          PROMOTION_MANIFEST_URI="${PROMOTION_MANIFEST_URI}" \
          COMPONENT_DEPLOY_TIMEOUT="${COMPONENT_DEPLOY_TIMEOUT}" \
          jenkins/scripts/component_deploy.sh kserve_model_cd
        '''
      }
    }
  }

  post {
    always {
      archiveArtifacts allowEmptyArchive: true, artifacts: '.model-cd/*'
      sh '''
        set +e
        if [ -n "${CI_TMP_ROOT:-}" ] && [ -d "${CI_TMP_ROOT}" ]; then
          rm -rf "${CI_TMP_ROOT}"
        fi
      '''
    }
  }
}
