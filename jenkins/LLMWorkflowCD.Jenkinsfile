pipeline {
  agent any
  options {
    skipDefaultCheckout(true)
    disableConcurrentBuilds()
    durabilityHint('MAX_SURVIVABILITY')
    lock(resource: 'recsys-production-release')
    timeout(time: 5, unit: 'HOURS')
  }
  parameters {
    choice(name: 'ACTION', choices: ['resume', 'activate', 'run', 'rollback', 'restore-baseline'], description: 'Activate deploys an IDLE baseline only. Restore is audited acceptance-only, not quarantine.')
    choice(name: 'EXPERIMENT_TYPE', choices: ['config_only', 'llm_only', 'combined'], description: 'Effective workflow change axes.')
    string(name: 'EXPERIMENT_ID', defaultValue: '', description: 'Idempotent workflow experiment ID.')
    string(name: 'BASELINE_RELEASE_ID', defaultValue: '', description: 'Expected immutable baseline; stale requests are rejected.')
    string(name: 'CANDIDATE_MANIFEST', defaultValue: '', description: 'Content-addressed MinIO candidate, never shell code.')
    choice(name: 'POLICY', choices: ['configs/llm-ab/workflow-production-policy.json', 'configs/llm-ab/workflow-live-test-policy.json'], description: 'No post-promotion monitor. Test traffic is not organic.')
  }
  environment {
    AB_SCOPE = 'workflow'
    AB_STATE_URI = 's3://recsys-llm-ab/workflow/state.json'
    AB_ROUTER_URL = 'http://recsys-workflow-router.kagent.svc.cluster.local'
    AB_SECRET_NAME = 'recsys-workflow-runtime'
    AB_ACTION = "${params.ACTION}"
    AB_MODE = "${params.EXPERIMENT_TYPE}"
    AB_EXPERIMENT_ID = "${params.EXPERIMENT_ID}"
    AB_EXPECTED_BASELINE = "${params.BASELINE_RELEASE_ID}"
    AB_CANDIDATE = "${params.CANDIDATE_MANIFEST}"
    AB_POLICY = "${params.POLICY}"
    AB_SOURCE_SHA256 = '__SOURCE_BUNDLE_SHA256__'
  }
  stages {
    stage('Load Pinned Workflow Runtime') {
      steps {
        deleteDir()
        script {
          env.AB_ROUTER_IMAGE = sh(returnStdout: true, script: "kubectl -n kagent get deployment recsys-workflow-router -o jsonpath='{.spec.template.spec.containers[0].image}'").trim()
        }
        withCredentials([file(credentialsId: 'recsys-workflow-source', variable: 'AB_SOURCE_BUNDLE')]) {
          sh '''
            set -eu
            printf '%s  %s\n' "$AB_SOURCE_SHA256" "$AB_SOURCE_BUNDLE" | sha256sum -c -
            tar -xzf "$AB_SOURCE_BUNDLE"
            # Dispatch/offline/live load share one finite execution capacity slot.
            # Read-only wait: never recreate or replay a dispatched Job.
            if [ "$AB_ACTION" = run ] && [ -n "${AB_EXPERIMENT_ID:-}" ]; then
              job="ab-dispatch-$AB_EXPERIMENT_ID"
              if [ -n "$(kubectl -n kagent get job "$job" --ignore-not-found -o name)" ]; then
                kubectl -n kagent wait --for=condition=complete "job/$job" --timeout=300s
              fi
            fi
            python3 -m venv .workflow-venv
            .workflow-venv/bin/pip install -r apps/agentic/llm_ab_router/requirements.txt
          '''
        }
      }
    }
    stage('Validate, Snapshot and Start') {
      steps {
        withCredentials([file(credentialsId: 'recsys-workflow-ab-env', variable: 'AB_ENV_FILE')]) {
          lock(resource: 'recsys-workflow-state') {
            sh '''
              set -eu
              .workflow-venv/bin/python -m jenkins.python.llm_agent_cd "$AB_ACTION" \
                --candidate "${AB_CANDIDATE:-}" --mode "$AB_MODE" --experiment-id "${AB_EXPERIMENT_ID:-}" \
                --policy "$AB_POLICY" --fixtures configs/llm-ab/workflow-cases-v11.json
            '''
          }
        }
      }
    }
    stage('Canary, 20 Conversations, Full Verification and Promote') {
      steps {
        script {
          def phase = ''
          while (!(phase in ['COMPLETED', 'ROLLED_BACK', 'ROLLBACK_FAILED', 'IDLE'])) {
            withCredentials([file(credentialsId: 'recsys-workflow-ab-env', variable: 'AB_ENV_FILE')]) {
              lock(resource: 'recsys-workflow-state') {
                sh '.workflow-venv/bin/python -m jenkins.python.llm_agent_cd resume'
              }
            }
            phase = sh(returnStdout: true, script: '''.workflow-venv/bin/python -c 'import json; print(json.load(open(".llm-agent-cd/status.json"))["phase"])' ''').trim()
            echo "Workflow phase: ${phase} (post-promotion monitoring disabled)"
            if (!(phase in ['COMPLETED', 'ROLLED_BACK', 'ROLLBACK_FAILED', 'IDLE'])) {
              sleep(time: 10, unit: 'SECONDS')
            }
          }
          if (phase in ['ROLLED_BACK', 'ROLLBACK_FAILED']) {
            currentBuild.result = 'UNSTABLE'
          }
        }
      }
    }
  }
  post {
    always { archiveArtifacts allowEmptyArchive: true, artifacts: '.llm-agent-cd/**/*.json' }
    unsuccessful { echo 'State remains authoritative. Reconcile before resuming; never replay a sent conversation.' }
  }
}
