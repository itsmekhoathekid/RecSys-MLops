def componentDefinitions() {
  return sh(
    returnStdout: true,
    script: 'python3 jenkins/python/configuration.py components-tsv'
  ).trim().split('\\n').findAll { it.trim() }.collect { line ->
    def fields = line.split('\\t', 3)
    [flag: fields[0], name: fields[1], label: fields[2]]
  }
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

def runComponentBranches(String scriptPath, String extraEnv, int maxParallel) {
  if (maxParallel < 1) {
    error 'maxParallel must be at least 1'
  }
  def selected = componentDefinitions().findAll { component ->
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

def runReleaseDeployPlan(String scriptPath, String extraEnv, String planPath) {
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
    componentsByToken.put(
      component.label.toLowerCase().replaceAll(/[^a-z0-9]+/, '_').replaceAll(/^_|_$/, ''),
      component
    )
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

  componentDefinitions().each { component -> env.setProperty(component.flag, 'false') }
  selectedByName.values().each { component -> env.setProperty(component.flag, 'true') }
  env.RUN_CI_CONFIG = forceCiConfig ? 'true' : 'false'
  env.RUN_COMPONENT_CI = selectedByName ? 'true' : 'false'
  env.RUN_COMPONENT_BUILD = selectedByName ? 'true' : 'false'
  env.RUN_COMPONENT_DEPLOY = selectedByName ? 'true' : 'false'
  env.RUN_PYTHON = selectedByName ? 'true' : 'false'
  def selectedNames = selectedByName.keySet().toList()
  sh(
    "python3 jenkins/python/release_plan.py create " +
    "--components '${selectedNames.join(',')}' " +
    "--commit '${env.GIT_COMMIT ?: ''}' " +
    "--output .ci-release-plan.json"
  )
  def forcedNames = selectedNames.toList()
  if (forceCiConfig) {
    forcedNames << 'ci_config'
  }
  env.CHANGED_COMPONENTS = forcedNames.join(',')
  echo "Forced CI/CD components: ${env.CHANGED_COMPONENTS}"
  return true
}

def shouldDeployChangedComponents() {
  def branchEnvironmentIsMain = [
    env.BRANCH_NAME,
    env.GIT_BRANCH
  ].findAll { it?.trim() }.any { branch ->
    branch == 'main' ||
      branch == 'origin/main' ||
      branch == 'refs/heads/main' ||
      branch == 'refs/remotes/origin/main'
  }
  def checkedOutCommitIsMain = gitRefExists('origin/main') && sh(
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
