def loadComponentDefinitions() {
  return sh(
    returnStdout: true,
    script: 'python3 jenkins/python/configuration.py components-tsv'
  ).trim().split('\\n').findAll { it.trim() }.collect { line ->
    def fields = line.split('\\t', 3)
    [flag: fields[0], name: fields[1], label: fields[2]]
  }
}

def gitCommitExists(String ref) {
  if (!ref?.trim() || ref ==~ /^0+$/) {
    return false
  }
  return sh(
    returnStatus: true,
    script: "git cat-file -e '${ref}^{commit}' >/dev/null 2>&1"
  ) == 0
}

def resolveDiffBase() {
  if (env.CHANGE_TARGET?.trim()) {
    def pullRequestBase = "origin/${env.CHANGE_TARGET}"
    if (gitCommitExists(pullRequestBase)) {
      return pullRequestBase
    }
  }
  for (String candidate : [env.GIT_PREVIOUS_COMMIT, env.GIT_PREVIOUS_SUCCESSFUL_COMMIT]) {
    if (gitCommitExists(candidate)) {
      return candidate
    }
  }
  return gitCommitExists('HEAD~1') ? 'HEAD~1' : ''
}

def runSelectedComponentCi(String scriptPath, String extraEnv, int maxParallel) {
  if (maxParallel < 1) {
    error 'maxParallel must be at least 1'
  }
  def selected = loadComponentDefinitions().findAll { component ->
    env.getProperty(component.flag) == 'true'
  }
  if (!selected) {
    echo 'No component changes detected for this stage.'
    return
  }
  selected.collate(maxParallel).eachWithIndex { batch, batchIndex ->
    def branches = [:]
    batch.each { component ->
      def componentName = component.name
      def componentLabel = component.label
      branches.put(componentLabel, {
        sh "${extraEnv} ${scriptPath} ${componentName}"
      })
    }
    echo "Running component CI batch ${batchIndex + 1} with ${branches.size()} branch(es)."
    parallel branches
  }
}

def deployReleasePlan(String scriptPath, String extraEnv, String planPath, String phase = 'all') {
  def rows = sh(
    returnStdout: true,
    script: "python3 jenkins/python/release_plan.py plan-units --plan '${planPath}' --phase '${phase}'"
  ).trim().split('\\n').findAll { it.trim() }.collect { line ->
    def fields = line.split('\\t', 3)
    [layer: fields[0] as Integer, name: fields[1], lockName: fields[2]]
  }
  rows.groupBy { it.layer }.keySet().sort().each { layer ->
    def branches = [:]
    rows.findAll { it.layer == layer }.each { unit ->
      def deployUnit = unit
      branches.put(deployUnit.name, {
        lock(resource: deployUnit.lockName) {
          sh "${extraEnv} ${scriptPath} '${deployUnit.name}' '${planPath}'"
        }
      })
    }
    if (branches) {
      parallel branches
    }
  }
}

def checkoutRevision() {
  sh 'timeout 30s git fetch --no-tags origin +refs/heads/*:refs/remotes/origin/* || true'
  env.GIT_COMMIT = sh(returnStdout: true, script: 'git rev-parse HEAD').trim()
}

def detectReleasePlan() {
  sh 'jenkins/scripts/maintenance/storage_preflight.sh'
  sh 'python3 jenkins/python/configuration.py validate'
  env.IMAGE_PUSH_REGISTRY = sh(
    returnStdout: true,
    script: 'python3 jenkins/python/configuration.py gcp imageRegistry'
  ).trim()
  env.IMAGE_PULL_REGISTRY = env.IMAGE_PUSH_REGISTRY
  env.CI_BASE_REF = resolveDiffBase()
  echo "Changed-path range: ${env.CI_BASE_REF ?: '<current commit>'}...HEAD"
  def baseArgument = env.CI_BASE_REF ? "--base-ref '${env.CI_BASE_REF}'" : ''
  withEnv(["FORCE_COMPONENTS_VALUE=${params.FORCE_COMPONENTS ?: ''}"]) {
    sh "python3 -m jenkins.python.change_detection.detector ${baseArgument} --force-components \"\${FORCE_COMPONENTS_VALUE}\" --commit '${env.GIT_COMMIT}' --plan-output .ci-release-plan.json > .ci-components.env"
  }
  readFile('.ci-components.env').split('\\n').each { line ->
    if (line.trim() && line.contains('=')) {
      def pair = line.split('=', 2)
      env.setProperty(pair[0], pair[1])
    }
  }
  env.SHOULD_PUBLISH_IMAGES = shouldPublishImages() ? 'true' : 'false'
  env.SHOULD_DEPLOY_RELEASE = shouldDeployRelease() ? 'true' : 'false'
  env.CI_TMP_ROOT = "/var/jenkins_home/ci-tmp/recsys-ci-${env.JOB_BASE_NAME}-${env.BUILD_NUMBER}"
  env.UV_CACHE_DIR = '/var/jenkins_home/caches/uv'
  echo "Selected components: ${env.CHANGED_COMPONENTS}"
  sh 'rm -rf reports .ci-image-manifest .ci-deploy pipelines/kubeflow/compiled/*.yaml && mkdir -p reports/junit reports/coverage .ci-image-manifest "${CI_TMP_ROOT}" "${UV_CACHE_DIR}"'
}

def preparePythonEnvironments() {
  sh '''#!/usr/bin/env bash
    set -euo pipefail
    jenkins/scripts/entrypoints/prepare_component_ci_envs.sh
  '''
}

def runComponentCi() {
  if (env.RUN_CI_CONFIG == 'true') {
    sh 'jenkins/scripts/entrypoints/ci_config.sh'
  }
  if (env.RUN_COMPONENT_CI == 'true') {
    def maxParallel = params.COMPONENT_CI_MAX_PARALLEL?.trim()?.toInteger()
    if (maxParallel < 1 || maxParallel > 13) {
      error 'COMPONENT_CI_MAX_PARALLEL must be between 1 and 13'
    }
    runSelectedComponentCi(
      'jenkins/scripts/entrypoints/component_ci.sh',
      "COVERAGE_MIN='${params.COVERAGE_MIN}'",
      maxParallel
    )
  }
}

def loginToRegistry() {
  sh '''#!/usr/bin/env bash
    set +x
    set -euo pipefail
    . jenkins/scripts/lib/common.sh
    . jenkins/scripts/deploy/preflight/gcp.sh
    . jenkins/scripts/lib/registry.sh
    gcp_verify_registry_publish_target
    registry_verify_gcp_upload_permission
    registry_login_gcp "${IMAGE_PUSH_REGISTRY}"
  '''
}

def buildAndPublish() {
  lock(resource: 'recsys-global-docker-build') {
    withEnv([
      "IMAGE_PUSH_REGISTRY=${env.IMAGE_PUSH_REGISTRY}",
      "IMAGE_TAG=${env.GIT_COMMIT ?: ''}",
      "PUBLISH_IMAGES=${env.SHOULD_PUBLISH_IMAGES == 'true' ? '1' : '0'}",
      "REQUIRE_GCP_ARTIFACT_REGISTRY=${env.SHOULD_PUBLISH_IMAGES == 'true' ? '1' : '0'}"
    ]) {
      sh 'jenkins/scripts/entrypoints/release_build_publish.sh .ci-release-plan.json'
      sh 'jenkins/scripts/entrypoints/release_package_artifacts.sh .ci-release-plan.json'
    }
  }
}

def releaseCommandEnvironment() {
  return "DEPLOY_TARGET='gcp-production' IMAGE_PULL_REGISTRY='${env.IMAGE_PULL_REGISTRY}' IMAGE_TAG='${env.GIT_COMMIT ?: ''}' FORCE_DEPLOY='${params.FORCE_DEPLOY ? '1' : '0'}' DEPLOY_PULL_REQUESTS='${params.DEPLOY_PULL_REQUESTS ? '1' : '0'}' PROMOTION_MANIFEST_URI='${params.PROMOTION_MANIFEST_URI}' AGENTIC_SMOKE_CHUNK_ID='${params.AGENTIC_SMOKE_CHUNK_ID}'"
}

def verifyRelease(String commandEnv) {
  if (env.RUN_DEMO_WEB == 'true' && params.GATEWAY_SMOKE_CREDENTIALS_ID?.trim()) {
    withCredentials([usernamePassword(credentialsId: params.GATEWAY_SMOKE_CREDENTIALS_ID, usernameVariable: 'GATEWAY_SMOKE_USER', passwordVariable: 'GATEWAY_SMOKE_PASSWORD')]) {
      sh "${commandEnv} jenkins/scripts/entrypoints/release_verify.sh .ci-release-plan.json"
    }
  } else {
    sh "${commandEnv} jenkins/scripts/entrypoints/release_verify.sh .ci-release-plan.json"
  }
}

def applyOptionalDatahubCutover(String commandEnv) {
  if (params.DATAHUB_CUTOVER_MODE == 'skip') {
    return
  }
  sh "${commandEnv} jenkins/scripts/entrypoints/datahub_cutover.sh plan .ci-deploy/datahub-dataset-lineage-cutover.json"
  if (params.DATAHUB_CUTOVER_MODE == 'apply') {
    def counts = readFile('.ci-deploy/datahub-dataset-lineage-cutover.json.counts').trim()
    input message: "Apply the archived DataHub soft-delete manifest? Targets: ${counts}", ok: 'Apply cutover'
    sh "${commandEnv} jenkins/scripts/entrypoints/datahub_cutover.sh apply .ci-deploy/datahub-dataset-lineage-cutover.json"
  }
}

def deployProductionRelease() {
  def commandEnv = releaseCommandEnvironment()
  sh "IMAGE_PULL_REGISTRY='${env.IMAGE_PULL_REGISTRY}' PUBLISH_IMAGES='${env.SHOULD_PUBLISH_IMAGES == 'true' ? '1' : '0'}' FORCE_DEPLOY='${params.FORCE_DEPLOY ? '1' : '0'}' DEPLOY_PULL_REQUESTS='${params.DEPLOY_PULL_REQUESTS ? '1' : '0'}' jenkins/scripts/entrypoints/release_deploy_preflight.sh .ci-release-plan.json"
  lock(resource: 'recsys-production-release') {
    sh "${commandEnv} jenkins/scripts/entrypoints/release_snapshot.sh .ci-release-plan.json"
    env.DEPLOY_STARTED = 'true'
    try {
      deployReleasePlan('jenkins/scripts/entrypoints/release_deploy_unit.sh', commandEnv, '.ci-release-plan.json', 'deploy')
      applyOptionalDatahubCutover(commandEnv)
      verifyRelease(commandEnv)
      deployReleasePlan('jenkins/scripts/entrypoints/release_deploy_unit.sh', commandEnv, '.ci-release-plan.json', 'finalize')
    } catch (Throwable originalFailure) {
      try {
        sh "${commandEnv} jenkins/scripts/entrypoints/release_rollback.sh .ci-release-plan.json"
      } catch (Throwable rollbackFailure) {
        echo "[ROLLBACK] failed while preserving original error: ${rollbackFailure}"
      }
      throw originalFailure
    }
  }
}

def isMissingWorkspaceContext(Throwable failure) {
  def current = failure
  while (current != null) {
    if (current.class.name.contains('MissingContextVariableException')) {
      return true
    }
    current = current.cause
  }
  return false
}

def safePostActions() {
  try {
    junit allowEmptyResults: true, testResults: 'reports/junit/*.xml'
    archiveArtifacts allowEmptyArchive: true, artifacts: 'reports/coverage/*.xml,reports/validation/**/*,reports/gcp/**/*,reports/agentic/**/*,pipelines/kubeflow/compiled/*.yaml,.ci-components.env,.ci-release-plan.json,.ci-image-manifest/*,.ci-deploy/**/*,.model-cd/*,.demo-web/**/*'
    sh 'jenkins/scripts/entrypoints/release_cleanup.sh'
  } catch (Throwable failure) {
    if (isMissingWorkspaceContext(failure)) {
      echo "[POST] workspace/launcher unavailable; evidence publication and cleanup skipped: ${failure.message}"
      return
    }
    echo "[POST] non-fatal cleanup/evidence error: ${failure}"
  }
}

def isMainRevision() {
  def branchEnvironmentIsMain = [
    env.BRANCH_NAME,
    env.GIT_BRANCH
  ].findAll { it?.trim() }.any { branch ->
    branch == 'main' ||
      branch == 'origin/main' ||
      branch == 'refs/heads/main' ||
      branch == 'refs/remotes/origin/main'
  }
  def checkedOutCommitIsMain = gitCommitExists('origin/main') && sh(
    returnStatus: true,
    script: 'test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"'
  ) == 0
  return branchEnvironmentIsMain || checkedOutCommitIsMain
}

def shouldPublishImages() {
  return params.PUBLISH_IMAGES && (
    params.DEPLOY_PULL_REQUESTS ||
    isMainRevision()
  )
}

def shouldDeployRelease() {
  return params.PUBLISH_IMAGES && env.RUN_COMPONENT_DEPLOY == 'true' && (
    params.DEPLOY_PULL_REQUESTS ||
    params.FORCE_DEPLOY ||
    isMainRevision()
  )
}

return this
