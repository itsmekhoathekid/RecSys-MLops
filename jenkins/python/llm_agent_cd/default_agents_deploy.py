"""Deploy the reviewed stock-ADK Context and A2A-only Coordinator defaults."""
import argparse
import json
import os
import re
import time
from pathlib import Path

from .driver import Driver, command
from .release import digest
from .release_guard import check
from .state import StateStore


RELEASES = (
    ("recsys-kagent-agent", Path("infra/helm/recsys-kagent-agent"),
     "recsys-context-agent-sandbox"),
    ("recsys-coordinator-agent", Path("infra/helm/recsys-coordinator-agent"),
     "recsys-coordinator-agent-sandbox"),
)
MCP_AUTH_VERSIONS = Path("configs/agentic/mcp-auth-versions.yaml")


def actor_runtime(driver, agent_name, expected_image):
    live=json.loads(driver.kube('get','sandboxagent',agent_name,'-o','json'))
    generation=str(live['metadata']['generation'])
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        templates=json.loads(driver.kube('get','actortemplates','-l',
            'kagent.dev/sandbox-agent='+agent_name,'-o','json'))['items']
        active=[item for item in templates if not item['metadata'].get('deletionTimestamp')
            and item['metadata'].get('annotations',{}).get('kagent.dev/desired-generation')==generation
            and item.get('status',{}).get('phase')=='Ready'
            and item['spec']['containers'][0]['image']==expected_image]
        if len(active)==1:
            return active[0]['metadata']['name']
        time.sleep(2)
    raise ValueError('default agent has no unique Ready ActorTemplate for its generation')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--image',required=True,
        help='Digest-pinned stock Go ADK image expected in regenerated ActorTemplates')
    args=parser.parse_args()
    if not re.fullmatch(r'.+/golang-adk@sha256:[0-9a-f]{64}',args.image):
        raise ValueError('stock Go ADK image must be digest-pinned')
    if not os.environ.get('BUILD_URL') or not os.environ.get('JENKINS_URL'):
        raise ValueError('Jenkins common-lock job required')
    os.environ.update(json.loads(Path(os.environ['AB_ENV_FILE']).read_text()))
    check()
    os.environ.update(AB_SCOPE='workflow',AB_SECRET_NAME='recsys-workflow-runtime',
        AB_ROUTER_IMAGE=json.loads(command('kubectl','-n','kagent','get','deployment',
            'recsys-workflow-router','-o','json'))['spec']['template']['spec']['containers'][0]['image'])
    driver=Driver()
    store=StateStore('s3://recsys-llm-ab/workflow/state.json')
    state,etag=store.read()
    if state['phase'] not in {'IDLE','COMPLETED','ROLLED_BACK'}:
        raise ValueError('active workflow experiment blocks default-agent deployment')
    if state.get('activated') and not driver.verify_route(
            state,state['verified_weight'],state['route_revision']):
        raise ValueError('existing workflow route is not verified')
    global_config=json.loads(driver.kube('get','modelconfig','recsys-global-model-config','-o','json'))
    global_checksum=digest(global_config['spec'])
    revisions={}
    for release,_,_ in RELEASES:
        status=json.loads(command('helm','status',release,'-n','kagent','-o','json'))
        if status['info']['status']!='deployed':
            raise ValueError(release+' has an unfinished Helm operation')
        revisions[release]=status['version']
    deployed=[]
    try:
        for release,chart,agent in RELEASES:
            # Reuse only operator-supplied values. --reuse-values can retain
            # computed defaults from an older chart and silently suppress the
            # reviewed prompt/tool change.
            values=command('helm','get','values',release,'-n','kagent','-o','json')
            helm_args = [
                'helm', 'upgrade', release, str(chart), '-n', 'kagent',
                '--reset-values', '-f', '-',
            ]
            if release == 'recsys-kagent-agent':
                # Keep the stable Context release bound to the reviewed
                # active MCP endpoint/Secret even when values are reset.
                helm_args.extend(('-f', str(MCP_AUTH_VERSIONS)))
            helm_args.extend(('--atomic', '--wait', '--timeout', '10m'))
            command(*helm_args, stdin=values)
            deployed.append(release)
            driver.kube('wait','--for=condition=Ready','sandboxagent/'+agent,'--timeout=300s')
            actor_runtime(driver,agent,args.image)
        context=json.loads(driver.kube('get','sandboxagent','recsys-context-agent-sandbox','-o','json'))
        coordinator=json.loads(driver.kube('get','sandboxagent','recsys-coordinator-agent-sandbox','-o','json'))
        context_prompt=context['spec']['declarative']['systemMessage']
        if ('null MUST remain' not in context_prompt or 'exactly one' not in context_prompt
                or 'never retry' not in context_prompt.lower()):
            raise ValueError('live Context exact-null/single-call prompt attestation failed')
        tools=coordinator['spec']['declarative']['tools']
        if (len(tools)!=2 or any(tool.get('type')!='Agent' for tool in tools)
                or sorted(tool['agent']['name'] for tool in tools)!=
                    ['recsys-context-agent-sandbox','recsys-recommendation-agent-sandbox']):
            raise ValueError('live Coordinator is not exactly two A2A tools')
        domains=coordinator['spec']['sandbox']['network']['allowedDomains']
        if (not any(domain.startswith('kagent-controller.kagent') for domain in domains)
                or any(domain in {'recsys-feature-rag-mcp.kagent.svc.cluster.local',
                                  'recsys-recommendation-mcp.kagent.svc.cluster.local'}
                       for domain in domains)):
            raise ValueError('live Coordinator network does not enforce A2A-only dependencies')
    except Exception:
        for release in reversed(deployed):
            command('helm','rollback',release,str(revisions[release]),'-n','kagent',
                '--wait','--timeout','10m')
        raise
    after,after_etag=store.read()
    current_global=json.loads(driver.kube('get','modelconfig','recsys-global-model-config','-o','json'))
    if after!=state or after_etag!=etag or digest(current_global['spec'])!=global_checksum:
        raise ValueError('state, champion, route or global ModelConfig changed during deploy')
    if state.get('activated') and not driver.verify_route(
            after,after['verified_weight'],after['route_revision']):
        raise ValueError('workflow route verification failed after default-agent deploy')
    report={'stage':'default_agents_deployed','build_url':os.environ['BUILD_URL'],
        'stock_adk_image':args.image,'context_exact_null_single_call':True,
        'coordinator_a2a_tools':['recsys-context-agent-sandbox','recsys-recommendation-agent-sandbox'],
        'coordinator_direct_mcp_tools':0,'state_unchanged':True,
        'global_model_config_unchanged':True,'synthetic_requests':0,'offline_requests':0}
    Path('.llm-agent-cd').mkdir(exist_ok=True)
    Path('.llm-agent-cd/evaluation-preparation.json').write_text(
        json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':
    main()
