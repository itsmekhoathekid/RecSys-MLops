"""Explicit approved Jenkins ACL migration; credentials never go to stdout.

Run with the project venv as a module. Each action is independently resumable.
Only existing local accounts retain admin; dispatch gets Read + job Read/Build
for the allowlisted LLM A/B pipelines and no other job.
"""
import argparse
from contextlib import contextmanager
import json
import requests
from jenkins.python.llm_agent_cd.provision import secret, forward, apply, kube

USER = 'recsys-workflow-dispatch'
JOBS = (
    'RecSys-LLM-Workflow-CD',
    'RecSys-LLM-Agent-CD',
    'RecSys-LLM-Candidate-Onboard',
)
SECRET = 'recsys-workflow-jenkins-dispatch'


@contextmanager
def client():
    auth = secret('ci', 'recsys-jenkins-admin')
    with forward('ci', 'recsys-jenkins', 8080) as url:
        s = requests.Session()
        s.trust_env = False
        s.auth = (auth['username'], auth['password'])
        r = s.get(url + '/crumbIssuer/api/json', timeout=20)
        r.raise_for_status()
        s.headers[r.json()['crumbRequestField']] = r.json()['crumb']
        yield s, url


def groovy(s, url, script, *, sensitive=False):
    r = s.post(url + '/scriptText', data={'script': script}, timeout=45)
    r.raise_for_status()
    try:
        return json.loads(r.text)
    except ValueError:
        if sensitive:
            raise RuntimeError('Jenkins script failed; sensitive response withheld') from None
        raise RuntimeError(r.text[:3000]) from None


def install():
    with client() as (s, url):
        return groovy(s, url, """
def j=jenkins.model.Jenkins.get()
def p=j.pluginManager.getPlugin('matrix-auth')
if (p != null) {
  assert p.version == '3.3'
  println(groovy.json.JsonOutput.toJson([installed:p.version,active:p.active]))
} else {
  assert j.queue.items.length == 0
  assert !j.getAllItems(hudson.model.Job).any { it.isBuilding() }
  def available=j.updateCenter.getPlugin('matrix-auth')
  assert available.version == '3.3'
  available.deploy(true)
  println(groovy.json.JsonOutput.toJson([installation:'submitted',version:available.version]))
}
""")


def migrate():
    with client() as (s, url):
        result = groovy(s, url, """
import jenkins.model.Jenkins
import hudson.model.User
import hudson.model.Item
import hudson.security.*
import org.jenkinsci.plugins.matrixauth.*
def j=Jenkins.get()
assert j.pluginManager.getPlugin('matrix-auth')?.active
assert j.securityRealm instanceof HudsonPrivateSecurityRealm
assert j.queue.items.length == 0
assert !j.getAllItems(hudson.model.Job).any { it.isBuilding() }
def jobs=['RecSys-LLM-Workflow-CD','RecSys-LLM-Agent-CD','RecSys-LLM-Candidate-Onboard'].collect { j.getItem(it) }
assert jobs.every { it != null }
assert jobs[0].description?.startsWith('Workflow A/B')
assert jobs[1].description?.startsWith('LLM Agent A/B production')
assert jobs[2].description?.startsWith('Generic GGUF candidate onboarding')
def existing=User.getAll().findAll { it.getProperty(HudsonPrivateSecurityRealm.Details) != null }.collect {it.id}
def strategy=j.authorizationStrategy
if (strategy instanceof FullControlOnceLoggedInAuthorizationStrategy) {
  assert User.getById('recsys-workflow-dispatch',false) == null
  def backup=new File(j.rootDir,'workflow-authorization-before-matrix.xml')
  assert !backup.exists()
  backup.text=Jenkins.XSTREAM2.toXML(strategy)
  strategy=new ProjectMatrixAuthorizationStrategy()
  existing.each { strategy.add(Jenkins.ADMINISTER,new PermissionEntry(AuthorizationType.USER,it)) }
  j.authorizationStrategy=strategy
  j.save()
}
assert strategy instanceof ProjectMatrixAuthorizationStrategy
existing.findAll {it != 'recsys-workflow-dispatch'}.each {
  assert strategy.rootACL.hasPermission2(User.getById(it,false).impersonate2(),Jenkins.ADMINISTER)
}
def entry=new PermissionEntry(AuthorizationType.USER,'recsys-workflow-dispatch')
strategy.add(Jenkins.READ,entry)
jobs.each { job ->
  def acl=job.getProperty(AuthorizationMatrixProperty)
  if (acl == null) acl=new AuthorizationMatrixProperty()
  acl.add(Item.READ,entry); acl.add(Item.BUILD,entry)
  job.addProperty(acl); job.save()
}
j.save()
def user=User.getById('recsys-workflow-dispatch',false)
if (user == null) {
  user=j.securityRealm.createAccount('recsys-workflow-dispatch',java.util.UUID.randomUUID().toString()+java.util.UUID.randomUUID().toString())
  user.description='Owned LLM A/B dispatcher; only allowlisted CD jobs read/build'
  user.save()
}
if (user.description == 'Owned workflow dispatcher; only Workflow-CD read/build') {
  user.description='Owned LLM A/B dispatcher; only allowlisted CD jobs read/build'
  user.save()
}
assert user.description == 'Owned LLM A/B dispatcher; only allowlisted CD jobs read/build'
def auth=user.impersonate2()
assert j.ACL.hasPermission2(auth,Jenkins.READ)
assert !j.ACL.hasPermission2(auth,Jenkins.ADMINISTER)
assert jobs.every { it.ACL.hasPermission2(auth,Item.READ) && it.ACL.hasPermission2(auth,Item.BUILD) && !it.ACL.hasPermission2(auth,Item.CONFIGURE) }
assert !j.getAllItems(hudson.model.Job).findAll {!jobs.contains(it)}.any {it.ACL.hasPermission2(auth,Item.READ) || it.ACL.hasPermission2(auth,Item.BUILD)}
println(groovy.json.JsonOutput.toJson([strategy:strategy.class.name,preserved_users:existing.findAll {it != 'recsys-workflow-dispatch'},dispatch:'recsys-workflow-dispatch',jobs:jobs*.name,verified:true]))
""")
    return result


def credential():
    old = kube('kagent', 'get', 'secret', SECRET, '--ignore-not-found', '-o', 'json')
    if old:
        current = json.loads(old)
        assert current['metadata'].get('labels', {}).get('recsys.ai/owner') == 'llm-agent-cd'
        # Job names are non-secret routing metadata. Reconcile newly managed
        # jobs without rotating the existing dispatcher token.
        desired = {
            'AB_JENKINS_JOB': JOBS[0],
            'AB_WORKFLOW_JENKINS_JOB': JOBS[0],
            'AB_RECOMMENDATION_JENKINS_JOB': JOBS[1],
            'AB_ONBOARDING_JENKINS_JOB': JOBS[2],
        }
        kube(
            'kagent', 'patch', 'secret', SECRET, '--type=merge', '-p',
            json.dumps({'stringData': desired}),
        )
        return {'credential': 'retained', 'job_routes_reconciled': True}
    with client() as (s, url):
        token = groovy(s, url, """
def j=jenkins.model.Jenkins.get()
assert j.authorizationStrategy instanceof hudson.security.ProjectMatrixAuthorizationStrategy
def u=hudson.model.User.getById('recsys-workflow-dispatch',false)
assert u.description == 'Owned LLM A/B dispatcher; only allowlisted CD jobs read/build'
assert !j.ACL.hasPermission2(u.impersonate2(),jenkins.model.Jenkins.ADMINISTER)
def p=u.getProperty(jenkins.security.ApiTokenProperty)
def existing=p.tokenStore.tokenList
assert existing.isEmpty() || (existing.size() == 1 && existing[0].name == 'workflow-dispatch-v1') : 'Unexpected dispatcher token requires operator reconciliation'
// The Kubernetes credential is confirmed absent before this script runs. An
// exactly-owned v1 token is therefore orphaned and cannot be recovered.
if (!existing.isEmpty()) p.tokenStore.revokeToken(existing[0].uuid)
def t=p.tokenStore.generateNewToken('workflow-dispatch-v1');u.save()
println(groovy.json.JsonOutput.toJson([token:t.plainValue]))
""", sensitive=True)
        apply('kagent', {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': SECRET,
            'labels': {'recsys.ai/owner': 'llm-agent-cd'}}, 'stringData': {
                'AB_JENKINS_USER': USER, 'AB_JENKINS_TOKEN': token['token'],
                'AB_JENKINS_URL': 'http://recsys-jenkins.ci.svc.cluster.local:8080',
                'AB_JENKINS_JOB': JOBS[0],
                'AB_WORKFLOW_JENKINS_JOB': JOBS[0],
                'AB_RECOMMENDATION_JENKINS_JOB': JOBS[1],
                'AB_ONBOARDING_JENKINS_JOB': JOBS[2]}})
    return {'credential': 'created', 'values_logged': False}


def verify():
    auth = secret('kagent', SECRET)
    with forward('ci', 'recsys-jenkins', 8080) as url:
        s = requests.Session()
        s.trust_env = False
        s.auth = (auth['AB_JENKINS_USER'], auth['AB_JENKINS_TOKEN'])
        results = {}
        for path, allowed in [('/api/json', True), ('/queue/api/json', True),
                *[('/job/' + job + '/api/json', True) for job in JOBS],
                *[('/job/' + job + '/config.xml', False) for job in JOBS],
                ('/job/RecSys-GitHub-CICD/api/json', False), ('/scriptText', False)]:
            r = s.get(url + path, timeout=20, allow_redirects=False)
            assert (r.status_code == 200) if allowed else (r.status_code in (403,404,405))
            results[path] = r.status_code
        return results


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['install', 'migrate', 'credential', 'verify'])
    print(json.dumps(globals()[p.parse_args().action]()))
