"""Operator-only Langfuse 4.17 webhook preparation. Never dispatches a run.

The version-pinned admin API is the same authenticated procedure used by its
UI. The generated HMAC secret goes directly to an owned Kubernetes Secret;
neither passwords, session cookies nor the webhook secret are logged.
"""
import json
import requests
from .provision import secret,apply,forward

PROJECT='recsys-production'
PROMPT='recsys-workflow-ab'
NAME='RecSys workflow A/B dispatch'
URL='https://agents.recsys-mlops.site/webhooks/langfuse'
SECRET='recsys-workflow-webhook-auth'


class Admin:
    def __init__(self,url,password):
        self.url=url
        self.http=requests.Session();self.http.trust_env=False
        r=self.http.get(url+'/api/auth/csrf',timeout=20);r.raise_for_status()
        csrf=r.json()['csrfToken']
        r=self.http.post(url+'/api/auth/callback/credentials',headers=self.headers(),data={
            'csrfToken':csrf,'email':'admin@recsys-mlops.site','password':password,
            'callbackUrl':'https://langfuse.recsys-mlops.site/','json':'true'},allow_redirects=False,timeout=30)
        r.raise_for_status()
        r=self.http.get(url+'/api/auth/session',headers=self.headers(),timeout=20);r.raise_for_status()
        if not r.json().get('user'):raise ValueError('Langfuse admin authentication failed')

    def headers(self):
        # Same service only, through authenticated Kubernetes port-forward.
        # Secure public-origin cookies are never forwarded to another host.
        return {'Cookie':'; '.join(k+'='+v for k,v in self.http.cookies.get_dict().items())}

    def call(self,method,payload,write=False):
        endpoint=self.url+'/api/trpc/automations.'+method
        if write:
            r=self.http.post(endpoint,headers=self.headers(),json={'json':payload},timeout=30)
        else:
            r=self.http.get(endpoint,headers=self.headers(),params={'input':json.dumps({'json':payload})},timeout=30)
        if r.status_code!=200:raise ValueError('Langfuse '+method+' rejected (HTTP '+str(r.status_code)+'); response withheld')
        return r.json()['result']['data']['json']


def config():
    return {'projectId':PROJECT,'name':NAME,'eventSource':'prompt','eventAction':['created','updated'],
        'filter':[{'column':'Name','type':'string','operator':'=','value':PROMPT}],
        'status':'INACTIVE','actionType':'WEBHOOK',
        'actionConfig':{'type':'WEBHOOK','url':URL,'apiVersion':{'prompt':'v1'},'requestHeaders':{}}}


def prepare():
    from .provision import kube
    old=kube('kagent','get','secret',SECRET,'--ignore-not-found','-o','json')
    if old and json.loads(old)['metadata'].get('labels',{}).get('recsys.ai/owner')!='llm-agent-cd':
        raise ValueError('foreign webhook secret; no automation changed')
    lf=secret('langfuse','recsys-langfuse-runtime')
    with forward('langfuse','langfuse-web',3000) as url:
        client=Admin(url,lf['initial-admin-password'])
        rows=client.call('getAutomations',{'projectId':PROJECT,'eventSource':'prompt'})
        owned=[a for a in rows if a['name']==NAME]
        if owned:
            if len(owned)!=1 or not old:raise ValueError('existing automation requires secret reconciliation; no duplicate created')
            a=owned[0];saved=secret('kagent',SECRET)
            if (a['id']!=saved['automation_id'] or a['action']['config']['url']!=URL
                    or a['trigger']['filter']!=config()['filter'] or a['trigger']['status']!='INACTIVE'):
                raise ValueError('automation drift; no update or secret rotation performed')
            return {'automation_id':a['id'],'status':'INACTIVE','prompt':PROMPT,'url':URL,'created':False}
        if old:raise ValueError('secret exists but automation missing; reconcile before recreation')
        # POST is never retried. A lost response leaves an operator-visible
        # automation to reconcile, not permission to create a duplicate.
        result=client.call('createAutomation',config(),write=True)
        generated=result.get('webhookSecret')
        if not isinstance(generated,str) or not generated:raise ValueError('HMAC secret missing; automation remains inactive')
        aid=result['automation']['id']
        apply('kagent',{'apiVersion':'v1','kind':'Secret','metadata':{'name':SECRET,
            'labels':{'recsys.ai/owner':'llm-agent-cd'}},'stringData':{
                'LANGFUSE_WEBHOOK_SECRET':generated,'automation_id':aid,'project_id':PROJECT,
                'prompt_name':PROMPT,'endpoint':URL}})
        return {'automation_id':aid,'status':'INACTIVE','prompt':PROMPT,'url':URL,'created':True}


def set_status(status):
    """Explicit operator action; retain HMAC and the reviewed exact filter."""
    if status not in {'ACTIVE','INACTIVE'}:raise ValueError('invalid status')
    saved=secret('kagent',SECRET)
    if status=='ACTIVE':
        dispatch=secret('kagent','recsys-workflow-trigger')
        if not dispatch or dispatch.get('AB_DISPATCH_ENABLED')!='true':
            raise ValueError('verified receiver dispatch must be enabled first')
    lf=secret('langfuse','recsys-langfuse-runtime')
    with forward('langfuse','langfuse-web',3000) as url:
        client=Admin(url,lf['initial-admin-password'])
        rows=client.call('getAutomations',{'projectId':PROJECT,'eventSource':'prompt'})
        owned=[a for a in rows if a['name']==NAME]
        if len(owned)!=1:raise ValueError('automation identity ambiguous')
        a=owned[0]
        if (a['id']!=saved['automation_id'] or a['action']['config']['url']!=URL
                or a['trigger']['filter']!=config()['filter']):raise ValueError('automation drift')
        if a['trigger']['status']!=status:
            client.call('updateAutomation',{**config(),'status':status,'automationId':a['id']},write=True)
        rows=client.call('getAutomations',{'projectId':PROJECT,'eventSource':'prompt'})
        if next(x for x in rows if x['id']==a['id'])['trigger']['status']!=status:
            raise ValueError('automation status not confirmed')
        return {'automation_id':a['id'],'status':status,'prompt':PROMPT}


if __name__=='__main__':print(json.dumps(prepare()))
