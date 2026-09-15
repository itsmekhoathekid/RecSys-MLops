from copy import deepcopy

from ops.gcp.forced_tool_diagnostic import diagnostic_requests, payload, verdict


def valid_response():
    return {'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [{
        'function': {'name': 'get_personalized_recommendations',
                     'arguments': '{"user_id":218,"candidate_item_ids":null,"top_k":3}'}}]}}]}


def test_diagnostic_changes_only_thinking_not_generation_or_tools():
    original = payload(None)
    revised = payload(False)
    assert revised.pop('chat_template_kwargs') == {'enable_thinking': False}
    assert revised == original
    assert original['max_tokens'] == 384
    assert original['tool_choice'] == 'required'
    assert len(original['tools']) == 1


def test_diagnostic_requires_one_real_structured_tool_call():
    good = valid_response()
    assert verdict(good)
    duplicate = deepcopy(good)
    duplicate['choices'][0]['message']['tool_calls'] *= 2
    assert not verdict(duplicate)
    truncated = deepcopy(good)
    truncated['choices'][0]['finish_reason'] = 'length'
    assert not verdict(truncated)
    invented = deepcopy(good)
    invented['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = '{"user_id":1001,"top_k":3}'
    assert not verdict(invented)
    assert not verdict({'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]})
    assert not verdict({})


def test_factor_isolation_does_not_change_generation():
    observed = payload(None)
    observed['messages'][0]['content'] = 'observed business prompt'
    observed['tools'][0]['function']['description'] = 'observed tool description'
    rows = diagnostic_requests(observed, isolate=True)
    assert len(rows) == 2
    assert rows[0][1]['messages'] == observed['messages']
    assert rows[0][1]['tools'] == payload(None)['tools']
    assert rows[1][1]['messages'] == payload(None)['messages']
    assert rows[1][1]['tools'] == observed['tools']
    for _, request in rows:
        assert request['max_tokens'] == 384 and request['temperature'] == 0


def test_prefill_supplies_only_format_not_arguments_or_tool_result():
    observed = payload(None)
    original = deepcopy(observed)
    rows = diagnostic_requests(observed, prefill=True)
    assert len(rows) == 1
    request = rows[0][1]
    assert observed == original
    assert request['messages'][:-1] == observed['messages']
    assert request['messages'][-1]['content'] == '<tool_call>\n<function=get_personalized_recommendations>\n'
    assert request['tools'] == observed['tools']
    assert request['add_generation_prompt'] is False
    assert request['continue_final_message'] == 'content'
