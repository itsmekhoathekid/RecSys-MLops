def componentDefinitions() {
  return [
    [flag: 'RUN_MATERIALIZE', name: 'materialize', label: 'Materialize Pipeline'],
    [flag: 'RUN_TRAINING', name: 'training', label: 'Training Pipeline'],
    [flag: 'RUN_SPARK_BATCH', name: 'spark_batch', label: 'Spark Batch Processing'],
    [flag: 'RUN_DP1', name: 'dp1', label: 'DP1 Raw To Bronze'],
    [flag: 'RUN_DP2', name: 'dp2', label: 'DP2 Bronze To Silver Gold'],
    [flag: 'RUN_DP3', name: 'dp3', label: 'DP3 Offline Feature Table'],
    [flag: 'RUN_API', name: 'api', label: 'FastAPI Web API'],
    [flag: 'RUN_KSERVE', name: 'kserve', label: 'KServe Inference Engine'],
    [flag: 'RUN_DRIFT', name: 'drift', label: 'Realtime Drift Detection'],
    [flag: 'RUN_STREAM_OFFLINE', name: 'stream_offline', label: 'Stream Features To Offline Store'],
    [flag: 'RUN_STREAM_ONLINE', name: 'stream_online', label: 'Stream Features To Online Store'],
    [flag: 'RUN_ANALYTICS', name: 'analytics', label: 'Analytics And BI'],
    [flag: 'RUN_DEMO_WEB', name: 'demo_web', label: 'Recommendation Demo Web'],
  ]
}

def gitRefExists(String ref) {
  if (!ref?.trim() || ref ==~ /^0+$/) {
    return false
  }
  return sh(
    returnStatus: true,
    script: "git cat-file -e '${ref}^{commit}' >/dev/null 2>&1"
  ) == 0
}

def resolveChangedBaseRef() {
  if (env.CHANGE_TARGET?.trim()) {
    def pullRequestBase = "origin/${env.CHANGE_TARGET}"
    if (gitRefExists(pullRequestBase)) {
      return pullRequestBase
    }
  }

  for (String candidate : [env.GIT_PREVIOUS_COMMIT, env.GIT_PREVIOUS_SUCCESSFUL_COMMIT]) {
    if (gitRefExists(candidate)) {
      return candidate
    }
  }

  return gitRefExists('HEAD~1') ? 'HEAD~1' : ''
}

def runComponentBranches(String scriptPath, String extraEnv) {
  def branches = [:]
  componentDefinitions().each { component ->
    if (env.getProperty(component.flag) == 'true') {
      def componentName = component.name
      def componentLabel = component.label
      branches.put(componentLabel, {
        sh "${extraEnv} ${scriptPath} ${componentName}"
      })
    }
  }
  if (branches) {
    parallel branches
  } else {
    echo 'No component changes detected for this stage.'
  }
}

def applyForcedComponents(String forcedComponents) {
  def requested = forcedComponents
    ?.split(',')
    ?.collect { it.trim().toLowerCase() }
    ?.findAll { it }

  if (!requested) {
    return false
  }

  def forceCiConfig = requested.contains('ci_config')
  requested = requested.findAll { it != 'ci_config' }

  def componentsByToken = [:]
  componentDefinitions().each { component ->
    componentsByToken.put(component.name, component)
    componentsByToken.put(component.flag.toLowerCase().replaceFirst('^run_', ''), component)
    componentsByToken.put(component.label.toLowerCase().replaceAll(/[^a-z0-9]+/, '_').replaceAll(/^_|_$/, ''), component)
  }

  def selectedByName = [:]
  def unknown = []
  requested.each { token ->
    def component = componentsByToken.get(token)
    if (component) {
      selectedByName.put(component.name, component)
    } else {
      unknown << token
    }
  }

  if (unknown) {
    error "Unknown FORCE_COMPONENTS token(s): ${unknown.join(', ')}"
  }

  componentDefinitions().each { component ->
    env.setProperty(component.flag, 'false')
  }
  selectedByName.values().each { component ->
    env.setProperty(component.flag, 'true')
  }

  env.RUN_CI_CONFIG = forceCiConfig ? 'true' : 'false'
  env.RUN_COMPONENT_CI = selectedByName ? 'true' : 'false'
  env.RUN_COMPONENT_BUILD = selectedByName ? 'true' : 'false'
  env.RUN_COMPONENT_DEPLOY = selectedByName ? 'true' : 'false'
  env.RUN_PYTHON = selectedByName ? 'true' : 'false'
  def forcedNames = selectedByName.keySet().toList()
  if (forceCiConfig) {
    forcedNames << 'ci_config'
  }
  env.CHANGED_COMPONENTS = forcedNames.join(',')
  echo "Forced CI/CD components: ${env.CHANGED_COMPONENTS}"
  return true
}

def shouldDeployChangedComponents() {
  return params.DEPLOY_CHANGED_COMPONENTS &&
    env.RUN_COMPONENT_DEPLOY == 'true' &&
    (
      params.FORCE_DEPLOY ||
      env.BRANCH_NAME == 'main' ||
      env.GIT_BRANCH == 'main' ||
      env.GIT_BRANCH == 'origin/main'
    )
}

pipeline {
  agent any

  options {
    skipDefaultCheckout(false)
  }

  parameters {
    string(name: 'IMAGE_PUSH_REGISTRY', defaultValue: 'asia-southeast1-docker.pkg.dev/fsds-coursework/recsys', description: 'Registry prefix used by Jenkins when pushing component images.')
    string(name: 'IMAGE_PULL_REGISTRY', defaultValue: 'asia-southeast1-docker.pkg.dev/fsds-coursework/recsys', description: 'Registry prefix used by Kubernetes workloads when pulling component images.')
    string(name: 'IMAGE_REGISTRY', defaultValue: '', description: 'Deprecated fallback used when IMAGE_PUSH_REGISTRY or IMAGE_PULL_REGISTRY is empty.')
    booleanParam(name: 'PUBLISH_IMAGES', defaultValue: true, description: 'Push images after successful component CI.')
    booleanParam(name: 'REQUIRE_GCP_ARTIFACT_REGISTRY', defaultValue: true, description: 'Fail build proof runs unless IMAGE_PUSH_REGISTRY points to GCP Artifact Registry and PUBLISH_IMAGES=true.')
    booleanParam(name: 'DEPLOY_CHANGED_COMPONENTS', defaultValue: true, description: 'Deploy/update changed components on main only.')
    booleanParam(name: 'FORCE_DEPLOY', defaultValue: false, description: 'Allow deploy/update from a non-main branch.')
    string(name: 'REGISTRY_CREDENTIALS_ID', defaultValue: '', description: 'Optional Jenkins username/password credential for docker login.')
    string(name: 'KUBECONFIG_CREDENTIALS_ID', defaultValue: '', description: 'Optional Jenkins file credential containing kubeconfig.')
    string(name: 'GATEWAY_SMOKE_CREDENTIALS_ID', defaultValue: '', description: 'Optional Jenkins username/password credential for authenticated demo web smoke.')
    string(name: 'PROMOTION_MANIFEST_URI', defaultValue: 's3://recsys-model-store/promotions/bst/latest.json', description: 'Production model manifest URI for KServe CD.')
    string(name: 'COVERAGE_MIN', defaultValue: '90', description: 'Minimum per-component unit coverage percentage.')
    string(name: 'FORCE_COMPONENTS', defaultValue: '', description: 'Comma-separated component names for manual proof jobs, including ci_config. Empty keeps path-based detection.')
  }

  environment {
    UV_LINK_MODE = 'copy'
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
        sh 'git fetch --no-tags origin +refs/heads/*:refs/remotes/origin/* || true'
      }
    }

    stage('Detect Changed Components') {
      steps {
        script {
          def baseRef = resolveChangedBaseRef()
          echo "Changed-path range: ${baseRef ?: '<current commit>'}...HEAD"
          def baseArgument = baseRef ? "--base-ref '${baseRef}'" : ''
          sh "python3 jenkins/scripts/detect_changed_components.py ${baseArgument} > .ci-components.env"
          readFile('.ci-components.env').split('\\n').each { line ->
            if (line.trim() && line.contains('=')) {
              def pair = line.split('=', 2)
              env.setProperty(pair[0], pair[1])
            }
          }
          if (!applyForcedComponents(params.FORCE_COMPONENTS ?: '')) {
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
            tests/unit/jenkins/test_detect_changed_components.py \
            -q \
            --junitxml=reports/junit/ci-config.xml
          python3 -m py_compile jenkins/scripts/detect_changed_components.py
          helm lint infra/helm/recsys-ci -f infra/helm/recsys-ci/values-gke.yaml
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
          runComponentBranches(
            'jenkins/scripts/component_ci.sh',
            "COVERAGE_MIN='${params.COVERAGE_MIN}'"
          )
        }
      }
    }

    stage('Docker Login') {
      when { expression { env.RUN_COMPONENT_BUILD == 'true' && params.PUBLISH_IMAGES } }
      steps {
        script {
          def pushRegistry = params.IMAGE_PUSH_REGISTRY?.trim() ?: params.IMAGE_REGISTRY
          def registryHost = pushRegistry.tokenize('/')[0]
          if (params.REGISTRY_CREDENTIALS_ID?.trim()) {
            withCredentials([usernamePassword(credentialsId: params.REGISTRY_CREDENTIALS_ID, usernameVariable: 'REGISTRY_USERNAME', passwordVariable: 'REGISTRY_PASSWORD')]) {
              sh "echo \"\\$REGISTRY_PASSWORD\" | docker login '${registryHost}' --username \"\\$REGISTRY_USERNAME\" --password-stdin"
            }
          } else if (registryHost.contains('.pkg.dev')) {
            sh """
              set +x
              set -euo pipefail
              token=\$(curl -fsS -H 'Metadata-Flavor: Google' 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"access_token\"])')
              echo "\$token" | docker login 'https://${registryHost}' --username oauth2accesstoken --password-stdin
            """
          } else {
            echo "No REGISTRY_CREDENTIALS_ID set and ${registryHost} is not GCP Artifact Registry; skipping docker login."
          }
        }
      }
    }

    stage('Component Build And Publish') {
      when { expression { env.RUN_COMPONENT_BUILD == 'true' } }
      steps {
        script {
          def pushRegistry = params.IMAGE_PUSH_REGISTRY?.trim() ?: params.IMAGE_REGISTRY
          runComponentBranches(
            'jenkins/scripts/component_build_publish.sh',
            "IMAGE_PUSH_REGISTRY='${pushRegistry}' IMAGE_TAG='${env.GIT_COMMIT ?: ''}' PUBLISH_IMAGES='${params.PUBLISH_IMAGES ? '1' : '0'}' REQUIRE_GCP_ARTIFACT_REGISTRY='${params.REQUIRE_GCP_ARTIFACT_REGISTRY ? '1' : '0'}'"
          )
        }
      }
    }

    stage('Component Deploy Or Update') {
      when { expression { shouldDeployChangedComponents() } }
      steps {
        script {
          def pullRegistry = params.IMAGE_PULL_REGISTRY?.trim() ?: params.IMAGE_REGISTRY
          def commandEnv = "IMAGE_PULL_REGISTRY='${pullRegistry}' IMAGE_TAG='${env.GIT_COMMIT ?: ''}' PROMOTION_MANIFEST_URI='${params.PROMOTION_MANIFEST_URI}'"
          if (params.KUBECONFIG_CREDENTIALS_ID?.trim()) {
            withCredentials([file(credentialsId: params.KUBECONFIG_CREDENTIALS_ID, variable: 'KUBECONFIG')]) {
              if (env.RUN_DEMO_WEB == 'true' && params.GATEWAY_SMOKE_CREDENTIALS_ID?.trim()) {
                withCredentials([usernamePassword(credentialsId: params.GATEWAY_SMOKE_CREDENTIALS_ID, usernameVariable: 'GATEWAY_SMOKE_USER', passwordVariable: 'GATEWAY_SMOKE_PASSWORD')]) {
                  runComponentBranches('jenkins/scripts/component_deploy.sh', commandEnv)
                }
              } else {
                runComponentBranches('jenkins/scripts/component_deploy.sh', commandEnv)
              }
            }
          } else if (env.RUN_DEMO_WEB == 'true' && params.GATEWAY_SMOKE_CREDENTIALS_ID?.trim()) {
            withCredentials([usernamePassword(credentialsId: params.GATEWAY_SMOKE_CREDENTIALS_ID, usernameVariable: 'GATEWAY_SMOKE_USER', passwordVariable: 'GATEWAY_SMOKE_PASSWORD')]) {
              runComponentBranches('jenkins/scripts/component_deploy.sh', commandEnv)
            }
          } else {
            runComponentBranches('jenkins/scripts/component_deploy.sh', commandEnv)
          }
        }
      }
    }
  }

  post {
    always {
      junit allowEmptyResults: true, testResults: 'reports/junit/*.xml'
      archiveArtifacts allowEmptyArchive: true, artifacts: 'reports/coverage/*.xml,reports/validation/**/*,infra/kubeflow/compiled/*.yaml,.ci-components.env,.ci-image-manifest/*,.model-cd/*'
      sh '''
        set +e
        if [ -n "${CI_TMP_ROOT:-}" ] && [ -d "${CI_TMP_ROOT}" ]; then
          rm -rf "${CI_TMP_ROOT}"
        fi
      '''
    }
  }
}
