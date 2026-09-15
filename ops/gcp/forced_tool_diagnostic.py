"""Bounded serving diagnostics, not offline or workflow acceptance.

Two different requests, one per setting, with fake tools and no tool executor.
Create-only claims prevent rerunning an ambiguous request. No deployment edits.
"""
import argparse
import json
from pathlib import Path
import time

import requests
from jenkins.python.llm_agent_cd.provision import forward, secret
from jenkins.python.llm_agent_cd.release import digest


def payload(thinking):
    body = {
        'model': 'qwen3.5-0.8b', 'temperature': 0.0, 'max_tokens': 384, 'seed': 42,
        'tool_choice': 'required', 'parallel_tool_calls': False,
        'messages': [
            {'role': 'system', 'content': 'Call get_personalized_recommendations once using the provided identifiers. Never invent identifiers.'},
            {'role': 'user', 'content': '{"user_id":218,"candidate_item_ids":null,"top_k":3}'},
        ],
        'tools': [{'type': 'function', 'function': {
            'name': 'get_personalized_recommendations',
            'description': 'Get recommendations for an explicitly provided user.',
            'parameters': {'type': 'object', 'properties': {
                'user_id': {'type': 'integer'},
                'candidate_item_ids': {'anyOf': [
                    {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1, 'maxItems': 500},
                    {'type': 'null'}], 'default': None},
                'top_k': {'type': 'integer', 'minimum': 1, 'maximum': 100}},
                'required': ['user_id', 'top_k']},
        }}],
    }
    if thinking is not None:
        body['chat_template_kwargs'] = {'enable_thinking': thinking}
    return body


def verdict(body):
    try:
        choice = body['choices'][0]
        calls = choice['message'].get('tool_calls') or []
        return (choice['finish_reason'] == 'tool_calls' and len(calls) == 1
                and calls[0]['function']['name'] == 'get_personalized_recommendations'
                and json.loads(calls[0]['function']['arguments']) == {
                    'user_id': 218, 'candidate_item_ids': None, 'top_k': 3})
    except (KeyError, TypeError, ValueError, IndexError):
        return False


def observed_request():
    """Reconstruct text/tool inputs from the known failed read-only trace.

    Only inference is diagnosed. No A2A request, task, or MCP execution is replayed.
    Generation and forced choice match the frozen runtime configuration.
    """
    credentials = secret('langfuse', 'recsys-langfuse-runtime')
    with forward('langfuse', 'langfuse-web', 3000) as url:
        with requests.Session() as client:
            client.trust_env = False
            client.auth = (credentials['project-public-key'], credentials['project-secret-key'])
            response = client.get(url + '/api/public/v2/observations', params={
                'traceId': 'c6b6a4ed2be5c73655df716fbffb12ee',
                'fromStartTime': '2026-09-09T16:58:00Z', 'toStartTime': '2026-09-09T17:03:00Z',
                'fields': 'core,basic,io', 'limit': 100}, timeout=30)
            response.raise_for_status()
            rows = [r for r in response.json()['data'] if r.get('type') == 'GENERATION']
            if len(rows) != 1:
                raise ValueError('exactly one source generation required')
            observed = rows[0]['input']
            if isinstance(observed, str):
                observed = json.loads(observed)
    body = payload(None)
    config = observed['Config']
    body['messages'] = [{'role': 'system', 'content': ''.join(
        p['text'] for p in config['systemInstruction']['parts'])}]
    for content in observed['Contents']:
        if content['role'] != 'user' or any(set(p) != {'text'} for p in content['parts']):
            raise ValueError('only the frozen text-only probe is allowed')
        body['messages'].append({'role': 'user', 'content': ''.join(p['text'] for p in content['parts'])})
    declarations = config['tools'][0]['functionDeclarations']
    if len(declarations) != 1 or declarations[0]['name'] != 'get_personalized_recommendations':
        raise ValueError('unexpected diagnostic tool')
    tool = declarations[0]
    body['tools'] = [{'type': 'function', 'function': {
        'name': tool['name'], 'description': tool['description'],
        'parameters': tool['parametersJsonSchema']}}]
    return body


def diagnostic_requests(observed=None, isolate=False, prefill=False):
    if prefill:
        if observed is None:
            raise ValueError('prefill diagnosis requires observed input')
        request = json.loads(json.dumps(observed))
        # The tool is already selected by the trusted runtime; only its XML
        # opener is supplied. All argument values must still be generated and
        # checked against the user request. This is not a production change.
        request['messages'].append({'role': 'assistant',
                                    'content': '<tool_call>\n<function=get_personalized_recommendations>\n'})
        request['continue_final_message'] = 'content'
        request['add_generation_prompt'] = False
        return [('required-tool-format-prefill', request)]
    if isolate:
        if observed is None:
            raise ValueError('factor isolation requires observed input')
        prompt_only = payload(None)
        prompt_only['messages'] = observed['messages']
        schema_only = payload(None)
        schema_only['tools'] = observed['tools']
        return [('observed-prompt-simple-schema', prompt_only),
                ('simple-prompt-observed-schema', schema_only)]
    result = []
    for name, thinking in [('default-thinking', None), ('disabled-thinking', False)]:
        request = json.loads(json.dumps(observed)) if observed is not None else payload(thinking)
        if thinking is not None:
            request['chat_template_kwargs'] = {'enable_thinking': thinking}
        result.append((name, request))
    return result


def run(output, use_observed=False, isolate=False, prefill=False):
    output.mkdir(exist_ok=False)
    observed = observed_request() if use_observed else None
    with forward('llm-inference', 'qwen35-gguf', 8000) as url:
        with requests.Session() as client:
            client.trust_env = False
            props = client.get(url + '/props', timeout=20)
            props.raise_for_status()
            info = props.json()
            identity = {'build_info': info.get('build_info'),
                        'template_sha256': digest(info.get('chat_template'))}
            for name, request in diagnostic_requests(observed, isolate, prefill):
                with (output / (name + '.intent.json')).open('x') as handle:
                    json.dump({'source': 'infrastructure_test', 'identity': identity,
                               'request': request, 'real_tool_executions': 0,
                               'offline_requests': 0, 'synthetic_requests': 0}, handle)
                start = time.monotonic()
                result = {'accepted': False, 'identity': identity, 'source': 'infrastructure_test'}
                try:
                    response = client.post(url + '/v1/chat/completions', json=request, timeout=120)
                    response.raise_for_status()
                    result['response'] = response.json()
                    result['accepted'] = verdict(result['response'])
                except (requests.RequestException, ValueError) as error:
                    result['error_type'] = type(error).__name__
                result['duration_seconds'] = time.monotonic() - start
                with (output / (name + '.result.json')).open('x') as handle:
                    json.dump(result, handle, indent=2)
                print(json.dumps({'case': name, 'accepted': result['accepted'],
                                  'duration_seconds': result['duration_seconds']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--observed-runtime-input', action='store_true')
    parser.add_argument('--isolate-factors', action='store_true')
    parser.add_argument('--format-prefill', action='store_true')
    args = parser.parse_args()
    if (args.isolate_factors or args.format_prefill) and not args.observed_runtime_input:
        parser.error('factor/prefill diagnostics require --observed-runtime-input')
    if args.isolate_factors and args.format_prefill:
        parser.error('choose one diagnostic mode')
    run(args.output, args.observed_runtime_input, args.isolate_factors, args.format_prefill)
