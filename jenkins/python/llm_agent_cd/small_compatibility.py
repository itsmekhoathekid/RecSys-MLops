"""Six direct inference checks with fabricated tool results; no real tool calls.

Not workflow acceptance, not synthetic A/B cases. No HTTP retries or top-up.
Writes each completed result before proceeding; refuses overwriting a prior run.
"""
import argparse
import json
from pathlib import Path
import re
import time
import requests
from .provision import forward
from .release import digest


SUITE_FILES = {
    'compatibility-smoke-v1': Path('configs/llm-ab/compatibility-smoke.json'),
    'recommendation-compatibility-smoke-v1': Path(
        'configs/llm-ab/recommendation-compatibility-smoke.json'
    ),
    'recommendation-compatibility-smoke-v2': Path(
        'configs/llm-ab/recommendation-compatibility-smoke-v2.json'
    ),
}


def smoke_case_ids(suite='compatibility-smoke-v1'):
    try:
        definition = json.loads(SUITE_FILES[suite].read_text())
    except KeyError as exc:
        raise ValueError('unknown compatibility suite') from exc
    ids = definition['case_ids']
    expected = {
        'compatibility-smoke-v1': [
            'tool_selection', 'arguments', 'composite_next_step'
        ],
        'recommendation-compatibility-smoke-v1': [
            'tool_selection', 'arguments', 'terminal_no_extra_call'
        ],
        'recommendation-compatibility-smoke-v2': [
            'tool_selection', 'arguments', 'null_arguments'
        ],
    }[suite]
    expected_name = {
        'compatibility-smoke-v1': 'workflow-compatibility-smoke-v1',
        'recommendation-compatibility-smoke-v1':
            'recommendation-compatibility-smoke-v1',
        'recommendation-compatibility-smoke-v2':
            'recommendation-compatibility-smoke-v2',
    }[suite]
    if ids != expected or definition.get('name') != expected_name:
        raise ValueError('compatibility smoke subset changed')
    return ids


def smoke_fixtures(contract, suite='compatibility-smoke-v1'):
    ids = smoke_case_ids(suite)
    source = recommendation_fixtures(contract) if suite.startswith('recommendation-compatibility-smoke-') else fixtures(contract)
    lookup = {name: (name, messages, assertion) for name, messages, assertion in source}
    return [lookup[name] for name in ids]


def generation_parameters(manifest):
    config = manifest.get('global_generation', manifest.get('config'))
    fields = {'temperature':'temperature', 'maxTokens':'max_tokens', 'seed':'seed',
              'topP':'top_p', 'frequencyPenalty':'frequency_penalty', 'presencePenalty':'presence_penalty'}
    if config is None or set(config) - set(fields):
        raise ValueError('unsupported or missing frozen generation config')
    result = {wire: config[name] for name, wire in fields.items() if name in config}
    result['temperature'] = float(result['temperature'])
    return result


TOOLS = [{"type": "function", "function": {"name": "get_recommendations",
    "description": "Get ranked items for an explicitly provided user ID.",
    "parameters": {"type": "object", "properties": {"user_id": {"type": "integer"}, "top_k": {"type": "integer"}},
                   "required": ["user_id", "top_k"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_context",
    "description": "Get context for ranked item IDs after recommendation has returned.",
    "parameters": {"type": "object", "properties": {"item_ids": {"type": "array", "items": {"type": "integer"}}},
                   "required": ["item_ids"], "additionalProperties": False}}}]

RECOMMENDATION_TOOLS = [{"type": "function", "function": {
    "name": "get_personalized_recommendations",
    "description": "Return ranked items for the explicitly provided user and candidates.",
    "parameters": {"type": "object", "properties": {
        "user_id": {"type": "integer", "minimum": 1},
        "candidate_item_ids": {"anyOf": [
            {"type": "array", "minItems": 1, "maxItems": 500,
             "items": {"type": "integer"}},
            {"type": "null"}
        ]},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 100}},
        "required": ["user_id", "candidate_item_ids", "top_k"],
        "additionalProperties": False}}}]


def smoke_tools(suite='compatibility-smoke-v1'):
    smoke_case_ids(suite)
    return RECOMMENDATION_TOOLS if suite.startswith('recommendation-compatibility-smoke-') else TOOLS


def recommendation_fixtures(contract):
    system = {"role": "system", "content": contract}
    ranked = {"items": [
        {"item_id": 7, "score": 0.8, "metadata": {"tag": "fixture"}},
        {"item_id": 4, "score": 0.5, "metadata": {}},
    ]}
    returned = [
        {"role": "user", "content": "Recommend 2 items for user_id=1001."},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "mock-call", "type": "function", "function": {
                "name": "get_personalized_recommendations",
                "arguments": '{"user_id":1001,"candidate_item_ids":null,"top_k":2}',
            }}]},
        {"role": "tool", "tool_call_id": "mock-call", "content": json.dumps(ranked)},
    ]
    return [
        ("tool_selection", [system, {"role": "user", "content":
            "Get recommendations with user_id=1001, candidate_item_ids=null, top_k=3."}],
            ("tool", "get_personalized_recommendations",
             {"user_id": 1001, "candidate_item_ids": None, "top_k": 3})),
        ("arguments", [system, {"role": "user", "content":
            "Recommend using user_id=1002, candidate_item_ids=[101,102], top_k=2."}],
            ("tool", "get_personalized_recommendations",
             {"user_id": 1002, "candidate_item_ids": [101, 102], "top_k": 2})),
        ("null_arguments", [system, {"role": "user", "content":
            "Recommend one item with user_id=1003, candidate_item_ids=null, top_k=1."}],
            ("tool", "get_personalized_recommendations",
             {"user_id": 1003, "candidate_item_ids": None, "top_k": 1})),
        ("terminal_no_extra_call", [system, *returned], ("terminal",)),
    ]


def fixtures(contract=None):
    system = {"role": "system", "content": "Use tools only when their required inputs are provided. Never invent user IDs. Preserve returned IDs, order, score and metadata exactly. Return final tool results as JSON only. Ask for user_id if missing."}
    if contract is not None:
        system["content"] = contract
    def user(text): return {"role": "user", "content": text}
    ranked = {"items": [{"item_id": 7, "score": 0.8, "metadata": {"tag": "fixture"}}, {"item_id": 4, "score": 0.5, "metadata": {}}]}
    def returned(value):
        return [user('Recommend 2 items for user_id=1001.'),
                {"role": "assistant", "content": None, "tool_calls": [{"id": "mock-call", "type": "function", "function": {"name": "get_recommendations", "arguments": '{"user_id":1001,"top_k":2}'}}]},
                {"role": "tool", "tool_call_id": "mock-call", "content": json.dumps(value)}]
    raw = [
        ('tool_selection', [user('Get recommendations for user_id=1001, top_k=3.')], ('tool', 'get_recommendations', {'user_id':1001,'top_k':3})),
        ('arguments', [user('Recommend exactly 2 items for user_id=1002.')], ('tool', 'get_recommendations', {'user_id':1002,'top_k':2})),
        ('tool_result', returned(ranked), ('json', ranked)),
        ('missing_user', [user('Recommend 2 items for me.')], ('missing',)),
        ('empty_result', returned({'items': []}), ('json', {'items': []})),
        ('composite_next_step', returned(ranked)+[user('Now call get_context for the returned item IDs in the same order; do not call recommendations again.')], ('tool','get_context',{'item_ids':[7,4]})),
    ]
    return [(name, [system]+messages, assertion) for name,messages,assertion in raw]


def check(message, assertion):
    calls = message.get('tool_calls') or []
    if assertion[0] == 'tool':
        return len(calls)==1 and calls[0]['function']['name']==assertion[1] and json.loads(calls[0]['function']['arguments'])==assertion[2]
    if calls:
        return False
    text = message.get('content') or ''
    if assertion[0] == 'terminal':
        return bool(text.strip())
    if assertion[0] == 'missing':
        return bool(re.search(r'user.?id',text,re.I))
    return json.loads(text.removeprefix('```json').removesuffix('```').strip()) == assertion[1]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',required=True)
    p.add_argument('--service', default='rec-llm-2c87bcf043928361e3fc')
    p.add_argument('--namespace', default='kagent')
    p.add_argument('--contract', choices=['legacy', 'workflow-json-safety-v1'], default='legacy')
    p.add_argument('--suite', choices=['six', 'smoke3'], default='six')
    p.add_argument('--generation-manifest')
    args=p.parse_args()
    if args.suite == 'smoke3' and not args.generation_manifest:
        p.error('smoke3 requires a frozen generation manifest')
    generation = generation_parameters(json.loads(Path(args.generation_manifest).read_text())) if args.generation_manifest else {'temperature':0,'max_tokens':384,'seed':42}
    output=Path(args.output)
    output.mkdir(exist_ok=False)
    report=[]
    with forward(args.namespace,args.service,8000) as endpoint:
        client=requests.Session()
        ready=client.get(endpoint+'/health',timeout=10)
        ready.raise_for_status()
        from .workflow_contract import SAFETY
        contract = SAFETY if args.contract != 'legacy' else None
        cases = smoke_fixtures(contract) if args.suite == 'smoke3' else fixtures(contract)
        fixture_checksum = digest(cases)
        for name,messages,assertion in cases:
            start=time.monotonic()
            row={'case':name,'namespace':args.namespace,'service':args.service,'contract':args.contract,'infrastructure_only':True,'real_tools_executed':0}
            row.update(suite=args.suite,fixture_checksum=fixture_checksum,generation=generation)
            # Persist a non-replayable claim before making any HTTP request.
            with (output/(name+'.intent.json')).open('x') as handle:
                json.dump({'case':name,'fixture_checksum':fixture_checksum,'status':'SENT_OR_AMBIGUOUS'},handle)
            try:
                response=client.post(endpoint+'/v1/chat/completions',json={
                    'model':'qwen3.5-0.8b','messages':messages,'tools':TOOLS,
                    **generation},timeout=120)
                response.raise_for_status()
                body=response.json()
                row['response']=body
                row['passed']=check(body['choices'][0]['message'],assertion)
            except (requests.RequestException,ValueError,KeyError,IndexError) as error:
                row.update(passed=False,error_type=type(error).__name__)
            row['duration_seconds']=time.monotonic()-start
            (output/(name+'.json')).write_text(json.dumps(row,indent=2)+'\n')
            report.append(row)
            print(name,'PASS' if row['passed'] else 'FAIL',round(row['duration_seconds'],2),flush=True)
    print('COMPATIBILITY',sum(r['passed'] for r in report),'/',len(cases),'; not workflow acceptance',flush=True)
    raise SystemExit(0 if all(r['passed'] for r in report) else 1)


if __name__=='__main__':
    main()
