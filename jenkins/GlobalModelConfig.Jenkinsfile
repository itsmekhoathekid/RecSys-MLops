pipeline {
  agent any
  options {
    skipDefaultCheckout(true)
    disableConcurrentBuilds()
    lock(resource: 'recsys-production-release')
    timeout(time: 45, unit: 'MINUTES')
  }
  parameters {
    text(name: 'VALUES_JSON', defaultValue: '', description: 'Operator-supplied Helm values JSON; not Langfuse input.')
  }
  environment {
    AB_SOURCE_SHA256 = '__SOURCE_BUNDLE_SHA256__'
  }
  stages {
    stage('Load pinned deploy source') {
      steps {
        deleteDir()
        withCredentials([file(credentialsId: 'recsys-workflow-source', variable: 'AB_SOURCE_BUNDLE')]) {
          sh '''
            set -eu
            printf '%s  %s\n' "$AB_SOURCE_SHA256" "$AB_SOURCE_BUNDLE" | sha256sum -c -
            tar -xzf "$AB_SOURCE_BUNDLE"
          '''
        }
      }
    }
    stage('Guard and deploy global defaults') {
      steps {
        writeFile file: '.global-model-config.json', text: params.VALUES_JSON
        withEnv(['RECSYS_GLOBAL_CONFIG_LOCKED=1']) {
          sh 'bash jenkins/scripts/deploy/global_model_config_locked.sh .global-model-config.json'
        }
      }
    }
  }
}
