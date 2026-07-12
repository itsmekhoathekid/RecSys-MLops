pipeline {
  agent any

  parameters {
    choice(name: 'ROLLOUT_STAGE', choices: ['deploy', 'shadow-start', 'ab-start', 'ab-step', 'evaluate', 'promote', 'rollback'], description: 'Champion/challenger lifecycle action.')
    string(name: 'PROMOTION_MANIFEST_URI', defaultValue: 's3://recsys-model-store/promotions/bst/latest.json', description: 'Stable production manifest updated on promotion.')
    string(name: 'CONTROL_MANIFEST_URI', defaultValue: '', description: 'Champion manifest. Defaults to PROMOTION_MANIFEST_URI.')
    string(name: 'CANDIDATE_MANIFEST_URI', defaultValue: '', description: 'Candidate manifest produced by Kubeflow promotion.')
    string(name: 'AB_EXPERIMENT_ID', defaultValue: '', description: 'Experiment label used by API and Prometheus gates.')
    string(name: 'AB_CANDIDATE_WEIGHT_PERCENT', defaultValue: '10', description: 'Candidate traffic for ab-start/ab-step/evaluate.')
    string(name: 'PROMETHEUS_URL', defaultValue: 'http://recsys-prometheus.observability.svc.cluster.local:9090', description: 'Prometheus endpoint for candidate gates.')
    string(name: 'AB_GATE_WINDOW', defaultValue: '10m', description: 'Prometheus comparison window.')
    string(name: 'AB_MIN_SAMPLES', defaultValue: '100', description: 'Minimum predictions required for each variant.')
    string(name: 'COMPONENT_DEPLOY_TIMEOUT', defaultValue: '600s', description: 'Helm/KServe rollout timeout.')
    string(name: 'MODEL_VERSION', defaultValue: '', description: 'Optional model version from Kubeflow.')
    string(name: 'METRIC_NAME', defaultValue: '', description: 'Backward-compatible Kubeflow promotion metric label.')
    string(name: 'METRIC_VALUE', defaultValue: '', description: 'Backward-compatible Kubeflow promotion metric value.')
    string(name: 'TRIGGER_SOURCE', defaultValue: 'manual', description: 'Caller that triggered this rollout action.')
  }

  environment {
    MODEL_CD_STAGE = "${params.ROLLOUT_STAGE}"
    CONTROL_MANIFEST_URI = "${params.CONTROL_MANIFEST_URI}"
    CANDIDATE_MANIFEST_URI = "${params.CANDIDATE_MANIFEST_URI}"
    AB_EXPERIMENT_ID = "${params.AB_EXPERIMENT_ID}"
    AB_CANDIDATE_WEIGHT_PERCENT = "${params.AB_CANDIDATE_WEIGHT_PERCENT}"
    PROMETHEUS_URL = "${params.PROMETHEUS_URL}"
    AB_GATE_WINDOW = "${params.AB_GATE_WINDOW}"
    AB_MIN_SAMPLES = "${params.AB_MIN_SAMPLES}"
  }

  stages {
    stage('Deploy Champion') {
      when { expression { params.ROLLOUT_STAGE == 'deploy' } }
      steps {
        sh 'MODEL_CD_STAGE=deploy jenkins/scripts/component_deploy.sh kserve_model_cd'
      }
    }

    stage('Deploy Shadow Candidate') {
      when { expression { params.ROLLOUT_STAGE == 'shadow-start' } }
      steps {
        echo "Starting shadow inference for ${params.CANDIDATE_MANIFEST_URI}; user traffic remains on champion."
        sh 'MODEL_CD_STAGE=shadow-start jenkins/scripts/component_deploy.sh kserve_model_cd'
      }
    }

    stage('Observe Shadow Candidate') {
      when { expression { params.ROLLOUT_STAGE == 'shadow-start' } }
      steps {
        sh '''
          set -euo pipefail
          kubectl get configmap recsys-api-serving -n api-serving \
            -o jsonpath='{.data.AB_SHADOW_ENABLED}'
          echo
          kubectl get configmap recsys-api-serving -n api-serving \
            -o jsonpath='{.data.AB_CANDIDATE_WEIGHT_PERCENT}'
          echo
          echo "Grafana proof metric: recsys_api_shadow_inferences_total"
        '''
      }
    }

    stage('Start Or Step A/B') {
      when { expression { params.ROLLOUT_STAGE in ['ab-start', 'ab-step'] } }
      steps {
        echo "Applying ${params.ROLLOUT_STAGE} at candidate weight ${params.AB_CANDIDATE_WEIGHT_PERCENT}%"
        sh 'jenkins/scripts/component_deploy.sh kserve_model_cd'
      }
    }

    stage('Evaluate Candidate') {
      when { expression { params.ROLLOUT_STAGE == 'evaluate' } }
      steps {
        sh '''
          set -euo pipefail
          MODEL_CD_STAGE=evaluate MODEL_CD_APPLY=0 jenkins/scripts/component_deploy.sh kserve_model_cd
          rm -f .model-cd/rollback-required
          if grep -q '"decision": "rollback"' .model-cd/ab-decision.json; then
            touch .model-cd/rollback-required
          fi
          python3 -m json.tool .model-cd/ab-decision.json
        '''
      }
    }

    stage('Promote Candidate') {
      when { expression { params.ROLLOUT_STAGE == 'promote' } }
      steps {
        sh 'MODEL_CD_STAGE=promote jenkins/scripts/component_deploy.sh kserve_model_cd'
      }
    }

    stage('Rollback Candidate') {
      when {
        anyOf {
          expression { params.ROLLOUT_STAGE == 'rollback' }
          expression { params.ROLLOUT_STAGE == 'evaluate' && fileExists('.model-cd/rollback-required') }
        }
      }
      steps {
        echo 'Candidate gate failed or rollback was requested; restoring champion-only traffic.'
        sh 'MODEL_CD_STAGE=rollback AB_CANDIDATE_WEIGHT_PERCENT=0 jenkins/scripts/component_deploy.sh kserve_model_cd'
      }
    }

    stage('Verify Champion Only') {
      when {
        anyOf {
          expression { params.ROLLOUT_STAGE == 'rollback' }
          expression { params.ROLLOUT_STAGE == 'evaluate' && fileExists('.model-cd/rollback-required') }
        }
      }
      steps {
        sh 'bash jenkins/scripts/verify_champion_only.sh'
      }
    }
  }

  post {
    always {
      archiveArtifacts allowEmptyArchive: true, artifacts: '.model-cd/*'
    }
  }
}
