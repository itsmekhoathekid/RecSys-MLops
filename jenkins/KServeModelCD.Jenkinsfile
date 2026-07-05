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

  stages {
    stage('Checkout') {
      steps {
        checkout scm
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
    }
  }
}
