from jenkins.python.llm_agent_cd.small_compatibility import fixtures, check
from jenkins.python.llm_agent_cd.small_compatibility import (
    smoke_fixtures,
    smoke_tools,
    generation_parameters,
)


def test_six_isolated_fixture_checks():
    cases=fixtures()
    assert len(cases)==6
    assert len({name for name,_,_ in cases})==6
    assert all(messages[0]['role']=='system' for _,messages,_ in cases)


def test_tool_assertion_checks_name_and_arguments():
    response={'tool_calls':[{'function':{'name':'get_context','arguments':'{"item_ids":[7,4]}'}}]}
    assert check(response,('tool','get_context',{'item_ids':[7,4]}))
    assert not check(response,('tool','get_context',{'item_ids':[4,7]}))
    assert not check(response,('missing',))


def test_empty_result_is_valid_not_an_error():
    assert check({'content':'{"items":[]}'},('json',{'items':[]}))
    assert not check({'content':'{"items":[1]}'},('json',{'items':[]}))


def test_smoke_is_exact_three_unchanged_assertions_and_frozen_generation():
    full = {name:(messages, assertion) for name,messages,assertion in fixtures('contract')}
    smoke = smoke_fixtures('contract')
    assert [c[0] for c in smoke] == ['tool_selection','arguments','composite_next_step']
    assert all(full[name] == (messages, assertion) for name,messages,assertion in smoke)
    assert generation_parameters({'global_generation':{'temperature':'0.2','maxTokens':384,'seed':42}}) == {'temperature':0.2,'max_tokens':384,'seed':42}


def test_recommendation_smoke_has_no_coordinator_or_context_step():
    smoke = smoke_fixtures('recommendation contract', 'recommendation-compatibility-smoke-v2')
    assert [c[0] for c in smoke] == [
        'tool_selection', 'arguments', 'null_arguments'
    ]
    assert all('get_context' not in str(messages) for _, messages, _ in smoke)
    tools = smoke_tools('recommendation-compatibility-smoke-v2')
    assert [tool['function']['name'] for tool in tools] == [
        'get_personalized_recommendations'
    ]
    candidate_schema = tools[0]['function']['parameters']['properties'][
        'candidate_item_ids'
    ]['anyOf'][0]
    assert candidate_schema['minItems'] == 1
    assert candidate_schema['maxItems'] == 500
    assert smoke[-1][2] == (
        'tool', 'get_personalized_recommendations',
        {'user_id': 1003, 'candidate_item_ids': None, 'top_k': 1},
    )
