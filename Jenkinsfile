def componentPipeline = null

pipeline {
  agent any

  options {
    skipDefaultCheckout(true)
  }

  parameters {
    booleanParam(name: 'PUBLISH_IMAGES', defaultValue: true, description: 'Push images after successful component CI.')
    booleanParam(name: 'FORCE_DEPLOY', defaultValue: false, description: 'Allow deploy/update from a non-main branch.')
    string(name: 'GATEWAY_SMOKE_CREDENTIALS_ID', defaultValue: '', description: 'Optional Jenkins username/password credential for authenticated demo web smoke.')
    string(name: 'PROMOTION_MANIFEST_URI', defaultValue: 's3://recsys-model-store/promotions/bst/latest.json', description: 'Production model manifest URI for KServe CD.')
    string(name: 'COVERAGE_MIN', defaultValue: '90', description: 'Minimum per-component unit coverage percentage.')
    string(name: 'FORCE_COMPONENTS', defaultValue: '', description: 'Comma-separated component names for manual proof jobs, including ci_config. Empty keeps path-based detection.')
  }

  environment {
    UV_LINK_MODE = 'copy'
    DEPLOY_TARGET = 'gcp-production'
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
        sh 'git fetch --no-tags origin +refs/heads/*:refs/remotes/origin/* || true'
        script {
          componentPipeline = load 'jenkins/pipeline/component_pipeline.groovy'
        }
      }
    }

    stage('Detect Changed Components') {
      steps {
        script {
          sh 'python3 jenkins/python/configuration.py validate'
          env.IMAGE_PUSH_REGISTRY = sh(
            returnStdout: true,
            script: 'python3 jenkins/python/configuration.py gcp imageRegistry'
          ).trim()
          env.IMAGE_PULL_REGISTRY = env.IMAGE_PUSH_REGISTRY
          def baseRef = componentPipeline.resolveChangedBaseRef()
          env.CI_BASE_REF = baseRef
          echo "Changed-path range: ${baseRef ?: '<current commit>'}...HEAD"
          def baseArgument = baseRef ? "--base-ref '${baseRef}'" : ''
          sh "python3 jenkins/scripts/detect_changed_components.py ${baseArgument} > .ci-components.env"
          readFile('.ci-components.env').split('\\n').each { line ->
            if (line.trim() && line.contains('=')) {
              def pair = line.split('=', 2)
              env.setProperty(pair[0], pair[1])
            }
          }
          if (!componentPipeline.applyForcedComponents(params.FORCE_COMPONENTS ?: '')) {
            echo "Changed components: ${env.CHANGED_COMPONENTS}"
          }
          // ML test environments can exceed the GKE node's ephemeral-storage
          // eviction threshold. Keep disposable CI data on the existing
          // Jenkins PVC; the post action removes this build-scoped directory.
          env.CI_TMP_ROOT = "/var/jenkins_home/ci-tmp/recsys-ci-${env.JOB_BASE_NAME}-${env.BUILD_NUMBER}"
          env.UV_PROJECT_ENVIRONMENT = "${env.CI_TMP_ROOT}/venv"
          env.UV_CACHE_DIR = "${env.CI_TMP_ROOT}/uv-cache"
          echo "Using CI temp root: ${env.CI_TMP_ROOT}"
        }
        sh 'rm -rf reports .ci-image-manifest && mkdir -p reports/junit reports/coverage .ci-image-manifest'
      }
    }

    stage('CI Configuration Validation') {
      when { expression { env.RUN_CI_CONFIG == 'true' } }
      steps {
        sh '''
          set -euo pipefail
          ci_config_venv="${CI_TMP_ROOT}/ci-config-venv"
          uv venv "${ci_config_venv}"
          uv pip install --python "${ci_config_venv}/bin/python" pytest
          "${ci_config_venv}/bin/python" -m pytest \
            tests/unit/jenkins \
            -q \
            --junitxml=reports/junit/ci-config.xml
          python3 -m compileall -q jenkins/python jenkins/scripts
          find jenkins/scripts infra/terraform/gcp/scripts \
            -type f -name '*.sh' -print0 | xargs -0 bash -n
          python3 jenkins/python/configuration.py validate
          for chart_file in infra/helm/*/Chart.yaml; do
            chart_dir="$(dirname "${chart_file}")"
            if [ -f "${chart_dir}/values-gcp.yaml" ]; then
              helm lint "${chart_dir}" -f "${chart_dir}/values-gcp.yaml"
              helm template validation "${chart_dir}" \
                -f "${chart_dir}/values-gcp.yaml" >/dev/null
            elif [ "${chart_dir}" = "infra/helm/recsys-ci" ]; then
              helm lint "${chart_dir}" -f "${chart_dir}/values-gke.yaml"
              helm template validation "${chart_dir}" \
                -f "${chart_dir}/values-gke.yaml" >/dev/null
            else
              helm lint "${chart_dir}"
              helm template validation "${chart_dir}" >/dev/null
            fi
          done
        '''
      }
    }

    stage('Python Env') {
      when { expression { env.RUN_PYTHON == 'true' } }
      steps {
        sh '''
          set -euo pipefail
          mkdir -p "${CI_TMP_ROOT}" "${UV_CACHE_DIR}"
          uv venv "${UV_PROJECT_ENVIRONMENT}"
          uv pip install --python "${UV_PROJECT_ENVIRONMENT}/bin/python" \
            pytest \
            pytest-cov \
            hypothesis \
            pyyaml \
            pydantic \
            numpy \
            pandas \
            pyarrow \
            "psycopg[binary]" \
            boto3 \
            requests \
            redis \
            fastapi \
            httpx \
            opentelemetry-api \
            opentelemetry-sdk \
            opentelemetry-exporter-otlp-proto-grpc \
            opentelemetry-instrumentation-fastapi \
            kfp \
            kubernetes \
            scikit-learn
          jenkins/scripts/install_component_ci_dependencies.sh
        '''
      }
    }

    stage('Component CI') {
      when { expression { env.RUN_COMPONENT_CI == 'true' } }
      steps {
        script {
          componentPipeline.runComponentBranches(
            'jenkins/scripts/component_ci.sh',
            "COVERAGE_MIN='${params.COVERAGE_MIN}'"
          )
        }
      }
    }

    stage('Docker Login') {
      when { expression { env.RUN_COMPONENT_BUILD == 'true' && params.PUBLISH_IMAGES } }
      steps {
        sh '''#!/usr/bin/env bash
          set +x
          set -euo pipefail
          . jenkins/scripts/lib/common.sh
          . jenkins/scripts/lib/gcp.sh
          . jenkins/scripts/lib/registry.sh
          python3 jenkins/python/configuration.py validate
          gcp_verify_production_target
          gcp_verify_workload_identity
          gcp_verify_required_crds
          gcp_verify_transaction_storage
          registry_verify_gcp_upload_permission
          registry_login_gcp "${IMAGE_PUSH_REGISTRY}"
        '''
      }
    }

    stage('Component Build And Publish') {
      when { expression { env.RUN_COMPONENT_BUILD == 'true' } }
      steps {
        script {
          componentPipeline.runComponentBranches(
            'jenkins/scripts/component_build_publish.sh',
            "IMAGE_PUSH_REGISTRY='${env.IMAGE_PUSH_REGISTRY}' IMAGE_TAG='${env.GIT_COMMIT ?: ''}' PUBLISH_IMAGES='${params.PUBLISH_IMAGES ? '1' : '0'}' REQUIRE_GCP_ARTIFACT_REGISTRY='1'"
          )
        }
      }
    }

    stage('Component Deploy Or Update') {
      when { expression { componentPipeline.shouldDeployChangedComponents() } }
      steps {
        script {
          env.DEPLOY_STARTED = 'true'
          def commandEnv = "DEPLOY_TARGET='gcp-production' IMAGE_PULL_REGISTRY='${env.IMAGE_PULL_REGISTRY}' IMAGE_TAG='${env.GIT_COMMIT ?: ''}' PUBLISH_IMAGES='${params.PUBLISH_IMAGES ? '1' : '0'}' FORCE_DEPLOY='${params.FORCE_DEPLOY ? '1' : '0'}' PROMOTION_MANIFEST_URI='${params.PROMOTION_MANIFEST_URI}'"
          if (env.RUN_DEMO_WEB == 'true' && params.GATEWAY_SMOKE_CREDENTIALS_ID?.trim()) {
            withCredentials([usernamePassword(credentialsId: params.GATEWAY_SMOKE_CREDENTIALS_ID, usernameVariable: 'GATEWAY_SMOKE_USER', passwordVariable: 'GATEWAY_SMOKE_PASSWORD')]) {
              componentPipeline.runComponentDeployBranches('jenkins/scripts/component_deploy.sh', commandEnv)
            }
          } else {
            componentPipeline.runComponentDeployBranches('jenkins/scripts/component_deploy.sh', commandEnv)
          }
        }
      }
    }
  }

  post {
    always {
      junit allowEmptyResults: true, testResults: 'reports/junit/*.xml'
      archiveArtifacts allowEmptyArchive: true, artifacts: 'reports/coverage/*.xml,reports/validation/**/*,reports/gcp/**/*,infra/kubeflow/compiled/*.yaml,.ci-components.env,.ci-image-manifest/*,.model-cd/*,.demo-web/**/*'
      sh '''
        set +e
        if [ "${DEPLOY_STARTED:-false}" = "true" ]; then
          old_ifs="${IFS}"
          IFS=','
          for component in ${CHANGED_COMPONENTS:-}; do
            [ -n "${component}" ] && [ "${component}" != "ci_config" ] || continue
            RECOVER_ONLY=1 \
              DEPLOY_TARGET=gcp-production \
              PUBLISH_IMAGES=1 \
              FORCE_DEPLOY=1 \
              IMAGE_PULL_REGISTRY="${IMAGE_PULL_REGISTRY}" \
              jenkins/scripts/component_deploy.sh "${component}" || exit $?
          done
          IFS="${old_ifs}"
        fi
        if [ -n "${CI_TMP_ROOT:-}" ] && [ -d "${CI_TMP_ROOT}" ]; then
          rm -rf "${CI_TMP_ROOT}"
        fi
      '''
    }
  }
}
