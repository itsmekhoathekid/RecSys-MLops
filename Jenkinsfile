def componentPipeline = null

pipeline {
  agent any

  options {
    disableConcurrentBuilds()
  }

  parameters {
    booleanParam(name: 'PUBLISH_IMAGES', defaultValue: true, description: 'Push images after successful component CI.')
    booleanParam(name: 'FORCE_DEPLOY', defaultValue: false, description: 'One-run override for deploy/update from a non-main branch.')
    booleanParam(name: 'DEPLOY_PULL_REQUESTS', defaultValue: false, description: 'Optional override to publish/deploy an unmerged pull-request branch; merged PR commits deploy through main by default.')
    string(name: 'COMPONENT_CI_MAX_PARALLEL', defaultValue: '2', description: 'Maximum component CI branches running in the Jenkins controller pod.')
    string(name: 'GATEWAY_SMOKE_CREDENTIALS_ID', defaultValue: '', description: 'Optional Jenkins username/password credential for authenticated demo web smoke.')
    string(name: 'PROMOTION_MANIFEST_URI', defaultValue: 's3://recsys-model-store/promotions/bst/latest.json', description: 'Production model manifest URI for KServe CD.')
    string(name: 'AGENTIC_SMOKE_CHUNK_ID', defaultValue: '800080:review:rev_800080_02:0', description: 'Known active chunk ID required by the grounded SandboxAgent A2A smoke test.')
    choice(name: 'DATAHUB_CUTOVER_MODE', choices: ['skip', 'plan', 'apply'], description: 'Optional one-time cleanup after static catalog deployment.')
    string(name: 'COVERAGE_MIN', defaultValue: '90', description: 'Minimum per-component unit coverage percentage.')
    string(name: 'FORCE_COMPONENTS', defaultValue: '', description: 'Comma-separated component names for manual proof jobs, including ci_config. Empty keeps path-based detection.')
  }

  environment {
    UV_LINK_MODE = 'copy'
    UV_CACHE_DIR = '/var/jenkins_home/caches/uv'
    DOCKER_BUILDKIT = '1'
    DOCKER_CLI_PLUGIN_EXTRA_DIRS = '/usr/local/lib/docker/cli-plugins'
    DEPLOY_TARGET = 'gcp-production'
  }

  stages {
    stage('Checkout') { // Resolve the exact source revision and load shared pipeline orchestration helpers.
      steps {
        script {
          componentPipeline = load 'jenkins/pipeline/component_pipeline.groovy' // Load diff-base, parallel-CI, deploy-order and deploy-eligibility helpers.
          componentPipeline.checkoutRevision() // Refresh refs without invalidating the already checked-out revision on a transient fetch timeout.
        }
      }
    }

    stage('Detect Changed Components') { // Convert the Git diff into component flags and an ordered CI/build/deploy release plan.
      steps {
        script {
          componentPipeline.detectReleasePlan() // Validate config, detect paths and write the immutable release plan plus RUN_* flags.
        }
      }
    }

    stage('Python Env') { // Materialize locked, profile-specific Python environments for the selected components.
      when { expression { env.RUN_PYTHON == 'true' } }
      steps {
        script {
          componentPipeline.preparePythonEnvironments() // Reuse locked profile environments and the persistent UV download cache.
        }
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
          componentPipeline.runComponentCi() // Run configuration contracts and selected components in batches of at most two branches.
        }
      }
    }

    stage('Docker Login') { // Authenticate to GCP Artifact Registry only when selected images will be published.
      when { expression { env.RUN_COMPONENT_BUILD == 'true' && env.SHOULD_PUBLISH_IMAGES == 'true' } }
      steps {
        script {
          componentPipeline.loginToRegistry() // Validate the production Artifact Registry target and create Docker credentials.
        }
      }
    }

    stage('Component Build And Publish') { // Build and optionally publish only the images/artifacts listed in the release plan.
      when { expression { env.RUN_COMPONENT_BUILD == 'true' } }
      steps {
        script {
          componentPipeline.buildAndPublish() // Serialize BuildKit, publish immutable digests and compile selected release artifacts.
        }
      }
    }

    stage('Component Deploy Or Update') { // Preflight, deploy dependency-ordered production units, then run component smoke verification.
      when { expression { env.SHOULD_DEPLOY_RELEASE == 'true' } }
      steps {
        script {
          componentPipeline.deployProductionRelease() // Snapshot, deploy, verify, publish registry metadata, and rollback on any failure or abort.
        }
      }
    }
  }

  post { // Publish evidence and clean build-scoped resources whether the pipeline succeeds or fails.
    always {
      script {
        if (componentPipeline != null) {
          componentPipeline.safePostActions() // Publish evidence and clean up only while workspace/launcher context remains available.
        } else {
          echo '[POST] pipeline helper was not loaded; skipping workspace-dependent post actions.'
        }
      }
    }
  }
}
