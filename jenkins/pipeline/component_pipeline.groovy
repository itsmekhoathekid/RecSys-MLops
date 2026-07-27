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

def runComponentDeployBranches(String scriptPath, String extraEnv) {
  def upstreamBranches = [:]
  componentDefinitions().each { component ->
    if (env.getProperty(component.flag) == 'true' && component.name != 'demo_web') {
      def componentName = component.name
      def componentLabel = component.label
      upstreamBranches.put(componentLabel, {
        sh "${extraEnv} ${scriptPath} ${componentName}"
      })
    }
  }
  if (upstreamBranches) {
    parallel upstreamBranches
  }
  if (env.RUN_DEMO_WEB == 'true') {
    sh "${extraEnv} ${scriptPath} demo_web"
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
  def forcedNames = selectedByName.keySet().toList()
  if (forceCiConfig) {
    forcedNames << 'ci_config'
  }
  env.CHANGED_COMPONENTS = forcedNames.join(',')
  echo "Forced CI/CD components: ${env.CHANGED_COMPONENTS}"
  return true
}

def shouldDeployChangedComponents() {
  return env.RUN_COMPONENT_DEPLOY == 'true' && (
    params.FORCE_DEPLOY ||
    env.BRANCH_NAME == 'main' ||
    env.GIT_BRANCH == 'main' ||
    env.GIT_BRANCH == 'origin/main'
  )
}

return this
