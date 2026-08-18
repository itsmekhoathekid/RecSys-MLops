def componentPipeline = null

pipeline {
  agent any

  options {
    disableConcurrentBuilds()
  }

  parameters {
    booleanParam(name: 'PUBLISH_IMAGES', defaultValue: true, description: 'Push images after successful component CI.')
    booleanParam(name: 'FORCE_DEPLOY', defaultValue: false, description: 'Allow deploy/update from a non-main branch.')
    string(name: 'COMPONENT_CI_MAX_PARALLEL', defaultValue: '3', description: 'Maximum component CI branches running in the Jenkins controller pod.')
    string(name: 'GATEWAY_SMOKE_CREDENTIALS_ID', defaultValue: '', description: 'Optional Jenkins username/password credential for authenticated demo web smoke.')
    string(name: 'PROMOTION_MANIFEST_URI', defaultValue: 's3://recsys-model-store/promotions/bst/latest.json', description: 'Production model manifest URI for KServe CD.')
    string(name: 'RAG_SOURCE_RUN_ID', defaultValue: '', description: 'Complete canonical RAG item-document run consumed by index promotion.')
    string(name: 'RAG_PIPELINE_RUN_ID', defaultValue: '', description: 'Unique silver/gold/index run ID used by RAG promotion and rollback.')
    choice(name: 'DATAHUB_CUTOVER_MODE', choices: ['skip', 'plan', 'apply'], description: 'Optional one-time cleanup after static catalog deployment.')
    string(name: 'COVERAGE_MIN', defaultValue: '90', description: 'Minimum per-component unit coverage percentage.')
    string(name: 'FORCE_COMPONENTS', defaultValue: '', description: 'Comma-separated component names for manual proof jobs, including ci_config. Empty keeps path-based detection.')
  }

  triggers {
    githubPush()
  }

  environment {
    UV_LINK_MODE = 'copy'
    DEPLOY_TARGET = 'gcp-production'
  }

  stages {
    stage('Checkout') { // Resolve the exact source revision and load shared pipeline orchestration helpers.
      steps {
        sh 'timeout 30s git fetch --no-tags origin +refs/heads/*:refs/remotes/origin/* || true' // Refresh diff refs, but keep the checked-out revision usable if the remote fetch times out.
        script {
          env.GIT_COMMIT = sh(
            returnStdout: true,
            script: 'git rev-parse HEAD'
          ).trim()
          componentPipeline = load 'jenkins/pipeline/component_pipeline.groovy' // Load diff-base, parallel-CI, deploy-order and deploy-eligibility helpers.
        }
      }
    }

    stage('Detect Changed Components') { // Convert the Git diff into component flags and an ordered CI/build/deploy release plan.
      steps {
        script {
          sh 'python3 jenkins/python/configuration.py validate' // Fail early when component, image or deploy-unit configuration is inconsistent.
          env.IMAGE_PUSH_REGISTRY = sh(
            returnStdout: true,
            script: 'python3 jenkins/python/configuration.py gcp imageRegistry'
          ).trim()
          env.IMAGE_PULL_REGISTRY = env.IMAGE_PUSH_REGISTRY
          def baseRef = componentPipeline.resolveDiffBase()
          env.CI_BASE_REF = baseRef
          echo "Changed-path range: ${baseRef ?: '<current commit>'}...HEAD"
          def baseArgument = baseRef ? "--base-ref '${baseRef}'" : ''
          withEnv(["FORCE_COMPONENTS_VALUE=${params.FORCE_COMPONENTS ?: ''}"]) {
            sh "python3 -m jenkins.python.change_detection.detector ${baseArgument} --force-components \"\${FORCE_COMPONENTS_VALUE}\" --commit '${env.GIT_COMMIT}' --plan-output .ci-release-plan.json > .ci-components.env" // Write the machine-readable release plan and shell-style RUN_* flags used by later stages.
          }
          readFile('.ci-components.env').split('\\n').each { line ->
            if (line.trim() && line.contains('=')) {
              def pair = line.split('=', 2)
              env.setProperty(pair[0], pair[1])
            }
          }
          echo "Selected components: ${env.CHANGED_COMPONENTS}"
          env.SHOULD_DEPLOY_RELEASE = componentPipeline.shouldDeployRelease() ? 'true' : 'false' // Deploy only published component changes from main, unless FORCE_DEPLOY explicitly overrides the branch gate.
          // ML test environments can exceed the GKE node's ephemeral-storage
          // eviction threshold. Keep disposable CI data on the existing
          // Jenkins PVC; the post action removes this build-scoped directory.
          env.CI_TMP_ROOT = "/var/jenkins_home/ci-tmp/recsys-ci-${env.JOB_BASE_NAME}-${env.BUILD_NUMBER}"
          env.UV_CACHE_DIR = "${env.CI_TMP_ROOT}/uv-cache"
          echo "Using CI temp root: ${env.CI_TMP_ROOT}"
        }
        sh 'rm -rf reports .ci-image-manifest .ci-deploy pipelines/kubeflow/compiled/*.yaml && mkdir -p reports/junit reports/coverage .ci-image-manifest' // Start this build with clean reports, manifests and deployment-preflight state.
      }
    }

    stage('Python Env') { // Materialize locked, profile-specific Python environments for the selected components.
      when { expression { env.RUN_PYTHON == 'true' } }
      steps {
        sh '''
          set -euo pipefail
          mkdir -p "${CI_TMP_ROOT}" "${UV_CACHE_DIR}"
          jenkins/scripts/entrypoints/prepare_component_ci_envs.sh # Reuse each prepared environment across component test branches with the same CI profile.
        '''
      }
    }

    stage('Component CI') { // Run configuration contracts plus selected component tests, coverage and migration-policy checks.
      when {
        expression {
          env.RUN_CI_CONFIG == 'true' || env.RUN_COMPONENT_CI == 'true'
        }
      }
      steps {
        script {
          if (env.RUN_CI_CONFIG == 'true') { // Validate Jenkins Python, shell syntax and every Helm chart before component-specific tests.
            echo '[CI] Contract checks'
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
              find jenkins/scripts ops \
                -type f -name '*.sh' -print0 | xargs -0 bash -n
              for chart_file in infra/helm/*/Chart.yaml; do
                chart_dir="$(dirname "${chart_file}")"
                if [ "${chart_dir}" = "infra/helm/recsys-rag-data" ]; then
                  # The one-shot generator chart intentionally requires a run ID;
                  # CI supplies a non-production sentinel only for template validation.
                  helm lint "${chart_dir}" -f "${chart_dir}/values-gcp.yaml"
                  helm template validation "${chart_dir}" \
                    -f "${chart_dir}/values-gcp.yaml" \
                    --set job.runId=ci-validation >/dev/null
                elif [ -f "${chart_dir}/values-gcp.yaml" ]; then
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
          if (env.RUN_COMPONENT_CI == 'true') { // Execute only diff-selected components, in bounded parallel batches.
            echo '[CI] Selected component branches'
            def maxParallel = params.COMPONENT_CI_MAX_PARALLEL?.trim()?.toInteger()
            if (maxParallel < 1 || maxParallel > 13) {
              error 'COMPONENT_CI_MAX_PARALLEL must be between 1 and 13'
            }
            componentPipeline.runSelectedComponentCi( // Each branch calls component_ci.sh and publishes its own JUnit/coverage files.
              'jenkins/scripts/entrypoints/component_ci.sh',
              "COVERAGE_MIN='${params.COVERAGE_MIN}'",
              maxParallel
            )
          }
        }
      }
    }

    stage('Docker Login') { // Authenticate to GCP Artifact Registry only when selected images will be published.
      when { expression { env.RUN_COMPONENT_BUILD == 'true' && params.PUBLISH_IMAGES } }
      steps {
        sh '''#!/usr/bin/env bash
          set +x
          set -euo pipefail
          . jenkins/scripts/lib/common.sh
          . jenkins/scripts/deploy/preflight/gcp.sh
          . jenkins/scripts/lib/registry.sh
          gcp_verify_registry_publish_target # Confirm the configured registry is the approved production publish target.
          registry_verify_gcp_upload_permission
          registry_login_gcp "${IMAGE_PUSH_REGISTRY}" # Create the Docker credential used by the following image pushes.
        '''
      }
    }

    stage('Component Build And Publish') { // Build and optionally publish only the images/artifacts listed in the release plan.
      when { expression { env.RUN_COMPONENT_BUILD == 'true' } }
      steps {
        echo '[BUILD] Build and publish catalog images'
        sh """
          IMAGE_PUSH_REGISTRY='${env.IMAGE_PUSH_REGISTRY}' \
          IMAGE_TAG='${env.GIT_COMMIT ?: ''}' \
          PUBLISH_IMAGES='${params.PUBLISH_IMAGES ? '1' : '0'}' \
          REQUIRE_GCP_ARTIFACT_REGISTRY='${params.PUBLISH_IMAGES ? '1' : '0'}' \
          jenkins/scripts/entrypoints/release_build_publish.sh .ci-release-plan.json # Produce immutable image references in .ci-image-manifest for deployment.
        """
        echo '[PACKAGE] Compile Kubeflow package'
        sh """
          IMAGE_PUSH_REGISTRY='${env.IMAGE_PUSH_REGISTRY}' \
          IMAGE_TAG='${env.GIT_COMMIT ?: ''}' \
          PUBLISH_IMAGES='${params.PUBLISH_IMAGES ? '1' : '0'}' \
          jenkins/scripts/entrypoints/release_package_artifacts.sh .ci-release-plan.json # Compile non-image release artifacts such as selected Kubeflow packages.
        """
      }
    }

    stage('Component Deploy Or Update') { // Preflight, deploy dependency-ordered production units, then run component smoke verification.
      when { expression { env.SHOULD_DEPLOY_RELEASE == 'true' } }
      steps {
        echo '[DEPLOY] Production preflight'
        sh "IMAGE_PULL_REGISTRY='${env.IMAGE_PULL_REGISTRY}' PUBLISH_IMAGES='${params.PUBLISH_IMAGES ? '1' : '0'}' FORCE_DEPLOY='${params.FORCE_DEPLOY ? '1' : '0'}' jenkins/scripts/entrypoints/release_deploy_preflight.sh .ci-release-plan.json" // Bind the approved GCP target and current commit before any production mutation.
        script {
          echo '[DEPLOY] Deploy release'
          env.DEPLOY_STARTED = 'true' // Mark that the build crossed from validation into production-changing work.
          def commandEnv = "DEPLOY_TARGET='gcp-production' IMAGE_PULL_REGISTRY='${env.IMAGE_PULL_REGISTRY}' IMAGE_TAG='${env.GIT_COMMIT ?: ''}' PROMOTION_MANIFEST_URI='${params.PROMOTION_MANIFEST_URI}' RAG_SOURCE_RUN_ID='${params.RAG_SOURCE_RUN_ID}' RAG_PIPELINE_RUN_ID='${params.RAG_PIPELINE_RUN_ID}'"
          componentPipeline.deployReleasePlan('jenkins/scripts/entrypoints/release_deploy_unit.sh', commandEnv, '.ci-release-plan.json') // Respect dependency layers and serialize units sharing the same Jenkins lock.
          if (params.DATAHUB_CUTOVER_MODE != 'skip') {
            sh "${commandEnv} jenkins/scripts/entrypoints/datahub_cutover.sh plan .ci-deploy/datahub-dataset-lineage-cutover.json"
            if (params.DATAHUB_CUTOVER_MODE == 'apply') {
              def cutoverCounts = readFile('.ci-deploy/datahub-dataset-lineage-cutover.json.counts').trim()
              input message: "Apply the archived DataHub soft-delete manifest? Targets: ${cutoverCounts}", ok: 'Apply cutover'
              sh "${commandEnv} jenkins/scripts/entrypoints/datahub_cutover.sh apply .ci-deploy/datahub-dataset-lineage-cutover.json"
            }
          }
          echo '[VERIFY] Verify release'
          if (env.RUN_DEMO_WEB == 'true' && params.GATEWAY_SMOKE_CREDENTIALS_ID?.trim()) {
            withCredentials([usernamePassword(credentialsId: params.GATEWAY_SMOKE_CREDENTIALS_ID, usernameVariable: 'GATEWAY_SMOKE_USER', passwordVariable: 'GATEWAY_SMOKE_PASSWORD')]) {
              sh "${commandEnv} jenkins/scripts/entrypoints/release_verify.sh .ci-release-plan.json" // Run authenticated smoke checks when the demo gateway requires credentials.
            }
          } else {
            sh "${commandEnv} jenkins/scripts/entrypoints/release_verify.sh .ci-release-plan.json" // Verify health, readiness, versions and runtime behavior after deployment; this is outside Helm atomic rollback.
          }
        }
      }
    }
  }

  post { // Publish evidence and clean build-scoped resources whether the pipeline succeeds or fails.
    always {
      junit allowEmptyResults: true, testResults: 'reports/junit/*.xml' // Render component and production-smoke results in the Jenkins test UI.
      archiveArtifacts allowEmptyArchive: true, artifacts: 'reports/coverage/*.xml,reports/validation/**/*,reports/gcp/**/*,pipelines/kubeflow/compiled/*.yaml,.ci-components.env,.ci-release-plan.json,.ci-image-manifest/*,.ci-deploy/**/*,.model-cd/*,.demo-web/**/*' // Preserve the release plan, exact images, index verification/rollback evidence, coverage, validation and deployment diagnostics as build proof.
      sh '''
        set +e
        if [ -n "${CI_TMP_ROOT:-}" ] && [ -d "${CI_TMP_ROOT}" ]; then
          rm -rf "${CI_TMP_ROOT}"
        fi
        jenkins/scripts/maintenance/docker_gc.sh
      '''
    }
  }
}
