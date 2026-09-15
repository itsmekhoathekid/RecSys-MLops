"""Submit an operator global deploy through the shared Jenkins release lock.

Never retries POST: an uncertain response requires queue/build reconciliation.
No direct Helm fallback, including when Jenkins is unavailable.
"""
import argparse
import json
from pathlib import Path
import requests
import yaml
from .provision import secret, forward


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('values', nargs='?', default='infra/helm/recsys-global-model-config/values.yaml')
    args = parser.parse_args()
    values = yaml.safe_load(Path(args.values).read_text())
    if not isinstance(values, dict) or set(values) != {'modelConfig'}:
        raise ValueError('only modelConfig Helm values are accepted')
    if values['modelConfig'].get('name') != 'recsys-global-model-config':
        raise ValueError('global resource name is fixed')
    encoded = json.dumps(values, allow_nan=False)
    if len(encoded.encode()) > 65536:
        raise ValueError('global values too large')
    admin = secret('ci', 'recsys-jenkins-admin')
    with forward('ci', 'recsys-jenkins', 8080) as endpoint:
        client = requests.Session()
        client.auth = (admin['username'], admin['password'])
        job = endpoint + '/job/RecSys-Global-Model-Config'
        status = client.get(job + '/api/json', timeout=15)
        status.raise_for_status()
        if not status.json().get('description', '').startswith('Workflow A/B global configuration'):
            raise ValueError('unexpected Jenkins job ownership')
        crumb = client.get(endpoint + '/crumbIssuer/api/json', timeout=15)
        crumb.raise_for_status()
        client.headers[crumb.json()['crumbRequestField']] = crumb.json()['crumb']
        try:
            response = client.post(job + '/buildWithParameters', data={'VALUES_JSON': encoded}, timeout=30, allow_redirects=False)
        except requests.RequestException:
            raise RuntimeError('Dispatch outcome unknown. Inspect Jenkins queue/build before another submission; no retry performed.') from None
        if response.status_code != 201 or not response.headers.get('Location'):
            raise RuntimeError('Dispatch not confirmed. Inspect Jenkins queue/build; no direct deploy or retry performed.')
        print('Global deploy queued; inspect RecSys-Global-Model-Config in Jenkins. Queue: ' + response.headers['Location'])


if __name__ == '__main__':
    main()
