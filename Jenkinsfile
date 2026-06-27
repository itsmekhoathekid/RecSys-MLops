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
  ]
}

def runComponentBranches(String scriptPath, String extraEnv) {
  def branches = [:]
  componentDefinitions().each { component ->
    if (env[component.flag] == 'true') {
      def componentName = component.name
      def componentLabel = component.label
      branches[componentLabel] = {
        sh "${extraEnv} ${scriptPath} ${componentName}"
      }
    }
  }
  if (branches) {
    parallel branches
  } else {
    echo 'No component changes detected for this stage.'
  }
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
    string(name: 'IMAGE_REGISTRY', defaultValue: 'localhost:5001/recsys', description: 'Registry prefix used for component images.')
    booleanParam(name: 'PUBLISH_IMAGES', defaultValue: true, description: 'Push images after successful component CI.')
    booleanParam(name: 'DEPLOY_CHANGED_COMPONENTS', defaultValue: true, description: 'Deploy/update changed components on main only.')
    booleanParam(name: 'FORCE_DEPLOY', defaultValue: false, description: 'Allow deploy/update from a non-main branch.')
    string(name: 'REGISTRY_CREDENTIALS_ID', defaultValue: '', description: 'Optional Jenkins username/password credential for docker login.')
    string(name: 'KUBECONFIG_CREDENTIALS_ID', defaultValue: '', description: 'Optional Jenkins file credential containing kubeconfig.')
    string(name: 'PROMOTION_MANIFEST_URI', defaultValue: 's3://recsys-model-store/promotions/bst/production.json', description: 'Production model manifest URI for KServe CD.')
    string(name: 'COVERAGE_MIN', defaultValue: '90', description: 'Minimum per-component unit coverage percentage.')
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
          def baseRef = env.CHANGE_TARGET ? "origin/${env.CHANGE_TARGET}" : 'HEAD~1'
          sh "python3 jenkins/scripts/detect_changed_components.py --base-ref '${baseRef}' > .ci-components.env"
          readFile('.ci-components.env').split('\\n').each { line ->
            if (line.trim() && line.contains('=')) {
              def pair = line.split('=', 2)
              env[pair[0]] = pair[1]
            }
          }
          echo "Changed components: ${env.CHANGED_COMPONENTS}"
        }
      }
    }

    stage('Python Env') {
      when { expression { env.RUN_PYTHON == 'true' } }
      steps {
        sh 'uv sync'
        sh 'mkdir -p reports/junit reports/coverage'
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
      when { expression { env.RUN_COMPONENT_BUILD == 'true' && params.PUBLISH_IMAGES && params.REGISTRY_CREDENTIALS_ID?.trim() } }
      steps {
        script {
          def registryHost = params.IMAGE_REGISTRY.tokenize('/')[0]
          withCredentials([usernamePassword(credentialsId: params.REGISTRY_CREDENTIALS_ID, usernameVariable: 'REGISTRY_USERNAME', passwordVariable: 'REGISTRY_PASSWORD')]) {
            sh "echo \"\\$REGISTRY_PASSWORD\" | docker login '${registryHost}' --username \"\\$REGISTRY_USERNAME\" --password-stdin"
          }
        }
      }
    }

    stage('Component Build And Publish') {
      when { expression { env.RUN_COMPONENT_BUILD == 'true' } }
      steps {
        script {
          runComponentBranches(
            'jenkins/scripts/component_build_publish.sh',
            "IMAGE_REGISTRY='${params.IMAGE_REGISTRY}' IMAGE_TAG='${env.GIT_COMMIT ?: ''}' PUBLISH_IMAGES='${params.PUBLISH_IMAGES ? '1' : '0'}'"
          )
        }
      }
    }

    stage('Component Deploy Or Update') {
      when { expression { shouldDeployChangedComponents() } }
      steps {
        script {
          def commandEnv = "IMAGE_REGISTRY='${params.IMAGE_REGISTRY}' IMAGE_TAG='${env.GIT_COMMIT ?: ''}' PROMOTION_MANIFEST_URI='${params.PROMOTION_MANIFEST_URI}'"
          if (params.KUBECONFIG_CREDENTIALS_ID?.trim()) {
            withCredentials([file(credentialsId: params.KUBECONFIG_CREDENTIALS_ID, variable: 'KUBECONFIG')]) {
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
      archiveArtifacts allowEmptyArchive: true, artifacts: 'reports/coverage/*.xml,infra/kubeflow/compiled/*.yaml,.ci-components.env,.ci-image-manifest/*,.model-cd/*'
    }
  }
}
