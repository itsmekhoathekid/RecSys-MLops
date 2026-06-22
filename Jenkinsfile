pipeline {
  agent any

  options {
    skipDefaultCheckout(false)
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

    stage('Detect Changes') {
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
      when { expression { env.RUN_PYTHON == 'true' || env.RUN_KFP == 'true' } }
      steps {
        sh 'uv sync'
        sh 'mkdir -p reports/junit'
      }
    }

    stage('Unit Tests') {
      parallel {
        stage('Data Generator') {
          when { expression { env.RUN_DATA_GENERATOR == 'true' } }
          steps {
            sh 'PYTHONPATH=apps/data-platform/data-generator/src uv run pytest tests/unit/data_generator -q --junitxml=reports/junit/data-generator.xml'
          }
        }

        stage('Data Platform') {
          when { expression { env.RUN_DATA_PLATFORM == 'true' } }
          steps {
            sh 'PYTHONPATH=apps/data-platform/src:apps/data-platform/data-generator/src uv run pytest tests/unit/data_platform tests/contract -q --junitxml=reports/junit/data-platform.xml'
          }
        }

        stage('Feature Store') {
          when { expression { env.RUN_FEATURE_STORE == 'true' } }
          steps {
            sh 'PYTHONPATH=apps/data-platform/src:apps/data-platform/feature-store/src uv run python -c "from pathlib import Path; assert Path(\\"apps/data-platform/feature-store/feature_repo/feature_store.yaml\\").exists(); import validate_feature_store; import feature_store.feast_registry"'
          }
        }

        stage('Kubeflow Utils') {
          when { expression { env.RUN_MODEL_PIPELINE == 'true' || env.RUN_KFP == 'true' } }
          steps {
            sh 'PYTHONPATH=apps/ml-system/src:apps/data-platform/src uv run pytest tests/unit/ml_system -q --junitxml=reports/junit/kubeflow-utils.xml'
          }
        }

        stage('API Scaffold') {
          when { expression { env.RUN_API == 'true' } }
          steps {
            sh 'test ! -d apps/api || find apps/api -maxdepth 2 -type f -print'
          }
        }
      }
    }

    stage('Kubeflow Compile') {
      when { expression { env.RUN_KFP == 'true' || env.RUN_MODEL_PIPELINE == 'true' } }
      steps {
        sh 'PYTHONPATH=apps/ml-system/src:apps/data-platform/src uv run python apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py'
      }
    }

    stage('Docker Dry-Run Builds') {
      when {
        expression {
          env.RUN_DOCKER_BASE == 'true' || env.RUN_DOCKER_DATA_GENERATOR == 'true' || env.RUN_DOCKER_DATAFLOW == 'true' || env.RUN_DOCKER_FEATURE_STORE == 'true' || env.RUN_DOCKER_TRAINING == 'true'
        }
      }
      steps {
        script {
          if (env.RUN_DOCKER_BASE == 'true' || env.RUN_DOCKER_DATA_GENERATOR == 'true' || env.RUN_DOCKER_FEATURE_STORE == 'true' || env.RUN_DOCKER_TRAINING == 'true') {
            sh 'docker build -f infra/docker/Dockerfile.base-python -t recsys-base-python:ci .'
          }
          def builds = [:]
          if (env.RUN_DOCKER_DATA_GENERATOR == 'true') {
            builds['data-generator'] = { sh 'docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:ci -f apps/data-platform/data-generator/Dockerfile -t recsys-data-generator:ci .' }
          }
          if (env.RUN_DOCKER_DATAFLOW == 'true') {
            builds['spark'] = { sh 'docker build -f apps/data-platform/Dockerfile.spark -t recsys-spark:ci .' }
            builds['flink'] = { sh 'docker build -f apps/data-platform/Dockerfile.flink -t recsys-flink:ci .' }
            builds['dataflow-cli'] = { sh 'docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:ci -f apps/data-platform/Dockerfile.dataflow-cli -t recsys-dataflow-cli:ci .' }
          }
          if (env.RUN_DOCKER_FEATURE_STORE == 'true') {
            builds['feature-store'] = { sh 'docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:ci -f apps/data-platform/feature-store/Dockerfile -t recsys-feature-store:ci .' }
          }
          if (env.RUN_DOCKER_TRAINING == 'true') {
            builds['training'] = { sh 'docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:ci -f apps/ml-system/Dockerfile.training -t recsys-mlops-training:ci .' }
          }
          if (builds) {
            parallel builds
          }
        }
      }
    }

    stage('Helm Dry-Run') {
      when { expression { env.RUN_HELM == 'true' } }
      steps {
        sh 'helm lint infra/helm/mlflow-stack && helm template recsys-mlflow infra/helm/mlflow-stack --namespace mlops >/tmp/recsys-mlflow.yaml'
        sh 'helm lint infra/helm/recsys-runtime && helm template recsys-runtime infra/helm/recsys-runtime --namespace kubeflow --set namespace.name=kubeflow >/tmp/recsys-runtime.yaml'
        sh 'helm lint infra/helm/ray-cluster && helm template recsys-ray-cpu infra/helm/ray-cluster --namespace kubeflow >/tmp/recsys-ray-cpu.yaml'
      }
    }
  }

  post {
    always {
      junit allowEmptyResults: true, testResults: 'reports/junit/*.xml'
      archiveArtifacts allowEmptyArchive: true, artifacts: 'infra/kubeflow/compiled/*.yaml,.ci-components.env'
    }
  }
}
