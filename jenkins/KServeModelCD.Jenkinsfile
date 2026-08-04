pipeline {
  agent any

  options {
    skipDefaultCheckout(true)
    disableConcurrentBuilds()
  }

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
    PROMOTION_MANIFEST_URI = "${params.PROMOTION_MANIFEST_URI}"
    CONTROL_MANIFEST_URI = "${params.CONTROL_MANIFEST_URI}"
    CANDIDATE_MANIFEST_URI = "${params.CANDIDATE_MANIFEST_URI}"
    AB_EXPERIMENT_ID = "${params.AB_EXPERIMENT_ID}"
    AB_CANDIDATE_WEIGHT_PERCENT = "${params.AB_CANDIDATE_WEIGHT_PERCENT}"
    PROMETHEUS_URL = "${params.PROMETHEUS_URL}"
    AB_GATE_WINDOW = "${params.AB_GATE_WINDOW}"
    AB_MIN_SAMPLES = "${params.AB_MIN_SAMPLES}"
  }

  stages {
    stage('Checkout Rollout Source') { // Check out the exact rollout code and fail early if the Model CD CLI or serving chart is missing.
      steps {
        checkout scm
        sh 'test -f jenkins/python/model_cd/cli.py && test -d infra/helm/recsys-serving && test -d infra/helm/recsys-inference-api'
      }
    }

    stage('Deploy Champion') { // Reconcile KServe and the API to a stable champion-only configuration.
      when { expression { params.ROLLOUT_STAGE == 'deploy' } }
      steps {
        lock(resource: 'helm:kserve-triton-inference:recsys-serving') {
          lock(resource: 'helm:api-serving:recsys-inference-api') {
            sh 'MODEL_CD_STAGE=deploy jenkins/scripts/entrypoints/model_cd_deploy.sh'
          }
        }
      }
    }

    stage('Deploy Shadow Candidate') { // Deploy the candidate and mirror payloads to it without returning candidate responses to users.
      when { expression { params.ROLLOUT_STAGE == 'shadow-start' } }
      steps {
        echo "Starting shadow inference for ${params.CANDIDATE_MANIFEST_URI}; user traffic remains on champion."
        lock(resource: 'helm:kserve-triton-inference:recsys-serving') {
          lock(resource: 'helm:api-serving:recsys-inference-api') {
            sh 'MODEL_CD_STAGE=shadow-start jenkins/scripts/entrypoints/model_cd_deploy.sh'
          }
        }
      }
    }

    stage('Observe Shadow Candidate') { // Prove the shadow flags in the live API ConfigMap and identify the Grafana metric to observe.
      when { expression { params.ROLLOUT_STAGE == 'shadow-start' } }
      steps {
        sh '''
          set -euo pipefail
          kubectl get configmap recsys-inference-api -n api-serving \
            -o jsonpath='{.data.AB_SHADOW_ENABLED}'
          echo
          kubectl get configmap recsys-inference-api -n api-serving \
            -o jsonpath='{.data.AB_CANDIDATE_WEIGHT_PERCENT}'
          echo
          echo "Grafana proof metric: recsys_api_shadow_inferences_total"
        '''
      }
    }

    stage('Start Or Step A/B') { // Start or change the deterministic percentage of user traffic routed to the candidate.
      when { expression { params.ROLLOUT_STAGE in ['ab-start', 'ab-step'] } }
      steps {
        echo "Applying ${params.ROLLOUT_STAGE} at candidate weight ${params.AB_CANDIDATE_WEIGHT_PERCENT}%"
        lock(resource: 'helm:kserve-triton-inference:recsys-serving') {
          lock(resource: 'helm:api-serving:recsys-inference-api') {
            sh 'jenkins/scripts/entrypoints/model_cd_deploy.sh'
          }
        }
      }
    }

    stage('Evaluate Candidate') { // Compare control/candidate samples, errors, p95 latency and quality without applying a new rollout state.
      when { expression { params.ROLLOUT_STAGE == 'evaluate' } }
      steps {
        sh '''
            set -euo pipefail
            MODEL_CD_STAGE=evaluate MODEL_CD_APPLY=0 jenkins/scripts/entrypoints/model_cd_deploy.sh # Write ab-decision.json in decision-only mode; do not apply Helm values.
            rm -f .model-cd/rollback-required # Remove any marker left by an earlier workspace run before reading this build's decision.
            if grep -q '"decision": "rollback"' .model-cd/ab-decision.json; then # Convert the JSON gate result into a Jenkins-readable conditional marker.
              touch .model-cd/rollback-required # Cause the later Rollback Candidate and Verify Champion Only stages to run in this build.
            fi
            python3 -m json.tool .model-cd/ab-decision.json
        '''
      }
    }

    stage('Promote Candidate') { // Recheck gates, move the candidate into the stable model location and restore champion-only routing.
      when { expression { params.ROLLOUT_STAGE == 'promote' } }
      steps {
        lock(resource: 'helm:kserve-triton-inference:recsys-serving') {
          lock(resource: 'helm:api-serving:recsys-inference-api') {
            sh 'MODEL_CD_STAGE=promote jenkins/scripts/entrypoints/model_cd_deploy.sh'
          }
        }
      }
    }

    stage('Rollback Candidate') { // Forward-deploy champion-only values after an explicit rollback or failed evaluation gate.
      when {
        anyOf {
          expression { params.ROLLOUT_STAGE == 'rollback' }
          expression { params.ROLLOUT_STAGE == 'evaluate' && fileExists('.model-cd/rollback-required') } // Auto-enter rollback only when this evaluation produced the marker.
        }
      }
      steps {
        echo 'Candidate gate failed or rollback was requested; restoring champion-only traffic.'
        lock(resource: 'helm:kserve-triton-inference:recsys-serving') {
          lock(resource: 'helm:api-serving:recsys-inference-api') {
            sh 'MODEL_CD_STAGE=rollback AB_CANDIDATE_WEIGHT_PERCENT=0 jenkins/scripts/entrypoints/model_cd_deploy.sh'
          }
        }
      }
    }

    stage('Verify Champion Only') { // Prove that configuration and live recommendation responses contain no candidate traffic.
      when {
        anyOf {
          expression { params.ROLLOUT_STAGE == 'promote' }
          expression { params.ROLLOUT_STAGE == 'rollback' }
          expression { params.ROLLOUT_STAGE == 'evaluate' && fileExists('.model-cd/rollback-required') }
        }
      }
      steps {
        sh 'bash jenkins/scripts/test/champion_only.sh' // Check weight/shadow flags, API rollout, 40 responses and the active champion version.
      }
    }
  }

  post { // Preserve rendered values and gate/deployment decisions even when rollout execution fails.
    always {
      archiveArtifacts allowEmptyArchive: true, artifacts: '.model-cd/**/*' // Include rendered values, decisions and pre-change release snapshots.
    }
  }
}
