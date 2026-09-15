"""runtime-json-v1: structural checks plus separate exact evidence assertions.

Additional returned metadata is preserved, never removed to make validation pass.
Context dicts are defined by the MCP API; composite context has typed boundaries.
"""
import math

VERSION = 'runtime-json-v1'


def validate(value, expected):
    if not isinstance(value,dict): return False
    if expected.get('missing_user'): return value=={'clarification':'Please provide user_id.'}
    roles = expected.get('trajectory',[])
    if len(roles)>1 and set(value)!=set(roles): return False
    for role in roles:
        output=value.get(role) if len(roles)>1 else value
        if not isinstance(output,dict): return False
        if role=='recommendation':
            if type(output.get('user_id')) is not int or output['user_id']<1: return False
            if not isinstance(output.get('model_version'),str) or not output['model_version']: return False
            if not isinstance(output.get('items'),list): return False
            for item in output['items']:
                if not isinstance(item,dict) or type(item.get('item_id')) is not int: return False
                score=item.get('score')
                if type(score) not in {int,float} or not math.isfinite(score): return False
        if role=='context' and expected.get('context',{}).get('tool')=='build_user_rag_context':
            if set(output)-{'user_context','rag_context','partial','errors'}: return False
            if not {'user_context','rag_context','partial','errors'}<=set(output): return False
            if any(output[k] is not None and not isinstance(output[k],dict) for k in ('user_context','rag_context')): return False
            if type(output['partial']) is not bool or not isinstance(output['errors'],list): return False
    return True
