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

def deployReleasePlan(String scriptPath, String extraEnv, String planPath) {
  def rows = sh(
    returnStdout: true,
    script: "python3 jenkins/python/release_plan.py plan-units --plan '${planPath}'"
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

def shouldDeployRelease() {
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
  return params.PUBLISH_IMAGES && env.RUN_COMPONENT_DEPLOY == 'true' && (
    params.FORCE_DEPLOY ||
    branchEnvironmentIsMain ||
    checkedOutCommitIsMain
  )
}

return this
