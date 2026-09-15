import pytest
from copy import deepcopy
from tests.unit.jenkins.test_llm_workflow import bundle
from tests.unit.jenkins.test_llm_agent_cd import champion
from jenkins.python.llm_agent_cd.workflow_contract import (
    revise_baseline,
    revise_coordinator_terminal_prompt,
    revise_coordinator_native_sequential_baseline,
    CONTRACT_VERSION,
    COORDINATOR_TERMINAL_CONTRACT_VERSION,
    COORDINATOR_SEQUENTIAL_CONTRACT_VERSION,
    SAFETY,
)
from jenkins.python.llm_agent_cd.workflow import prompt_checksum, validate_workflow
from jenkins.python.llm_agent_cd.small_compatibility import fixtures


def test_migration_is_separate_baseline_preserving_config_llm_and_tools(bundle):
    original = bundle
    before = deepcopy(original)
    revised, audit = revise_baseline(original)
    assert original == before
    assert revised['release_id'] != original['release_id']
    assert revised['config_id'] == original['config_id']
    assert revised['llm_version_id'] == original['llm_version_id']
    assert revised['bindings'] == original['bindings']
    for role in original['agents']:
        assert revised['agents'][role]['tools'] == original['agents'][role]['tools']
        assert revised['agents'][role]['systemMessage'].startswith(original['agents'][role]['systemMessage'])
        assert CONTRACT_VERSION in revised['agents'][role]['systemMessage']
    assert audit['parent_release_id'] == original['release_id']
    with pytest.raises(ValueError):
        validate_workflow(original, revised, 'config_only')
    with pytest.raises(ValueError, match='already present'):
        revise_baseline(revised)


def test_coordinator_terminal_migration_changes_only_prompt_and_freezes_checksum(bundle):
    from tests.unit.jenkins.test_baseline_cutover import active_stock_revision

    _legacy, baseline, _audit = active_stock_revision(bundle)
    before = deepcopy(baseline)
    revised, audit = revise_coordinator_terminal_prompt(baseline)
    assert baseline == before
    assert revised["release_id"] != baseline["release_id"]
    for field in ("config_id", "llm_version_id", "global_generation", "agent_overrides",
                  "llm", "bindings", "runtime"):
        assert revised[field] == baseline[field]
    for role in ("context", "recommendation"):
        assert revised["agents"][role] == baseline["agents"][role]
    assert revised["agents"]["coordinator"]["tools"] == baseline["agents"]["coordinator"]["tools"]
    prompt = revised["agents"]["coordinator"]["systemMessage"]
    assert COORDINATOR_TERMINAL_CONTRACT_VERSION in prompt
    assert "with one Recommendation response: call no function" in prompt
    assert "with one Context response: call no function" in prompt
    assert prompt_checksum(revised) == audit["prompt_checksum"]
    assert audit["control_candidate_prompt_checksum"] == audit["prompt_checksum"]
    with pytest.raises(ValueError, match="already applied"):
        revise_coordinator_terminal_prompt(revised)


def test_native_sequential_baseline_is_shared_by_both_ab_arms(bundle):
    import json
    from pathlib import Path
    from tests.unit.jenkins.test_baseline_cutover import active_stock_revision
    from apps.agentic.llm_ab_router.trigger import candidate_from_config
    from jenkins.python.llm_agent_cd.release import digest

    _legacy, stock, _audit = active_stock_revision(bundle)
    v30, _v30_audit = revise_coordinator_terminal_prompt(stock)
    before = deepcopy(v30)
    revised, audit = revise_coordinator_native_sequential_baseline(v30)
    assert v30 == before
    assert revised["release_id"] != v30["release_id"]
    for field in ("config_id", "llm_version_id", "global_generation", "agent_overrides",
                  "llm", "bindings", "runtime"):
        assert revised[field] == v30[field]
    for role in ("context", "recommendation"):
        assert revised["agents"][role] == v30["agents"][role]
    assert all("isolateSessions" not in tool
               for tool in revised["agents"]["coordinator"]["tools"])
    assert COORDINATOR_SEQUENTIAL_CONTRACT_VERSION in (
        revised["agents"]["coordinator"]["systemMessage"])
    assert "builtin/a2a-communication" not in revised["agents"]["coordinator"]["systemMessage"]
    assert "consider retrying" not in revised["agents"]["coordinator"]["systemMessage"].lower()
    assert audit["coordinator_isolate_sessions"] is False
    assert audit["builtin_a2a_prompt_included"] is False
    assert audit["native_go_sandbox_agent"] is True

    candidate_llm = json.loads(Path(
        "configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m-tools-v3.json"
    ).read_text())
    config = {
        "schema_version": 1,
        "scope": "workflow",
        "baseline_workflow_release_id": revised["release_id"],
        "global_generation": deepcopy(revised["global_generation"]),
        "llm_release_ref": digest(candidate_llm),
        "experiment_type": "llm_only",
        "policy_ref": "workflow-production",
        "target_role": "coordinator",
    }
    candidate = candidate_from_config(revised, config, candidate_llm)
    assert candidate["agents"] == revised["agents"]
    assert all("isolateSessions" not in tool
               for tool in candidate["agents"]["coordinator"]["tools"])
    with pytest.raises(ValueError, match="already applied"):
        revise_coordinator_native_sequential_baseline(revised)


def test_diagnostic_revision_changes_only_system_not_cases_or_assertions():
    old, new = fixtures(), fixtures(SAFETY)
    assert len(new) == 6
    for (name, messages, assertion), (name2, messages2, assertion2) in zip(old, new):
        assert (name, assertion, messages[1:]) == (name2, assertion2, messages2[1:])
        assert messages2[0]['content'] == SAFETY


def test_fixture_v2_only_remaps_verified_users_and_keeps_exact_twenty_cases():
    import hashlib,json,re
    from pathlib import Path
    root=Path('configs/llm-ab')
    provenance=json.loads((root/'workflow-cases-v2.provenance.json').read_text())
    old=(root/provenance['parent_fixture_file']).read_bytes()
    new=(root/provenance['fixture_file']).read_bytes()
    assert hashlib.sha256(old).hexdigest()==provenance['parent_fixture_file_sha256']
    assert hashlib.sha256(new).hexdigest()==provenance['fixture_file_sha256']
    assert len(json.loads(old))==len(json.loads(new))==20
    remapped=re.sub(r'\b(1001|1002|1003|1004|1005|1006)\b',
        lambda m:str(provenance['user_mapping'][m.group()]),old.decode())
    assert json.loads(remapped)==json.loads(new)


def test_fixture_v3_only_makes_existing_null_candidate_constraint_explicit():
    import hashlib,json
    from pathlib import Path
    root=Path('configs/llm-ab')
    provenance=json.loads((root/'workflow-cases-v3.provenance.json').read_text())
    old_bytes=(root/provenance['parent_fixture_file']).read_bytes()
    new_bytes=(root/provenance['fixture_file']).read_bytes()
    assert hashlib.sha256(old_bytes).hexdigest()==provenance['parent_fixture_file_sha256']
    assert hashlib.sha256(new_bytes).hexdigest()==provenance['fixture_file_sha256']
    old,new=json.loads(old_bytes),json.loads(new_bytes)
    assert len(old)==len(new)==20
    changed=[]
    for before,after in zip(old,new):
        assert before['id']==after['id']
        prompt=before.pop('prompt'); revised=after.pop('prompt')
        assert before==after
        if prompt!=revised:
            changed.append(before['id'])
            assert before['expected']['recommendation']['arguments']['candidate_item_ids'] is None
            assert 'candidate_item_ids=null' in revised
    assert changed==[*(f'workflow-{i:02d}' for i in range(1,7)),
                     *(f'workflow-{i:02d}' for i in range(11,17))]


def test_fixture_v4_makes_nested_a2a_requests_explicit_without_changing_assertions():
    import hashlib,json
    from pathlib import Path
    root=Path('configs/llm-ab')
    provenance=json.loads((root/'workflow-cases-v4.provenance.json').read_text())
    old_bytes=(root/provenance['parent_fixture_file']).read_bytes()
    new_bytes=(root/provenance['fixture_file']).read_bytes()
    assert hashlib.sha256(old_bytes).hexdigest()==provenance['parent_fixture_file_sha256']
    assert hashlib.sha256(new_bytes).hexdigest()==provenance['fixture_file_sha256']
    old,new=json.loads(old_bytes),json.loads(new_bytes)
    assert len(old)==len(new)==20
    for before,after in zip(old,new):
        assert before['id']==after['id']
        assert {k:v for k,v in before.items() if k!='prompt'}=={k:v for k,v in after.items() if k!='prompt'}
        if before['group'] not in {'missing_user'}:
            assert 'exact JSON string:' in after['prompt']
        else:
            assert before['prompt']==after['prompt']


def test_fixture_v5_bounds_context_payload_without_relaxing_twenty_case_gate():
    import hashlib,json
    from pathlib import Path
    root=Path('configs/llm-ab')
    provenance=json.loads((root/'workflow-cases-v5.provenance.json').read_text())
    old_bytes=(root/provenance['parent_fixture_file']).read_bytes()
    new_bytes=(root/provenance['fixture_file']).read_bytes()
    assert hashlib.sha256(old_bytes).hexdigest()==provenance['parent_fixture_file_sha256']
    assert hashlib.sha256(new_bytes).hexdigest()==provenance['fixture_file_sha256']
    old,new=json.loads(old_bytes),json.loads(new_bytes)
    assert len(old)==len(new)==20
    assert [c['id'] for c in old]==[c['id'] for c in new]
    assert {g:sum(c['group']==g for c in new) for g in {c['group'] for c in new}} == {
        'recommendation':6,'context':4,'composite':4,'limits':2,
        'missing_user':2,'empty':2}
    for case in new:
        expected=case['expected']
        if case['group'] in {'context','composite'}:
            assert expected['context']=={
                'tool':'get_chunk_by_id',
                'arguments':{'chunk_id':'800080:review:rev_800080_02:0'}}
        if case['group'] not in {'missing_user'}:
            assert case['prompt'].startswith('ROUTE=')
    assert provenance['gate_relaxed'] is False


def test_fixture_v6_uses_native_tool_friendly_commands_without_changing_expected_values():
    import hashlib,json
    from pathlib import Path
    root=Path('configs/llm-ab')
    provenance=json.loads((root/'workflow-cases-v6.provenance.json').read_text())
    old_bytes=(root/provenance['parent_fixture_file']).read_bytes()
    new_bytes=(root/provenance['fixture_file']).read_bytes()
    assert hashlib.sha256(old_bytes).hexdigest()==provenance['parent_fixture_file_sha256']
    assert hashlib.sha256(new_bytes).hexdigest()==provenance['fixture_file_sha256']
    old,new=json.loads(old_bytes),json.loads(new_bytes)
    assert len(old)==len(new)==20
    for before,after in zip(old,new):
        assert before['id']==after['id']
        assert before['group']==after['group']
        assert before['expected']==after['expected']
        if after['group'] not in {'missing_user'}:
            assert 'ROUTE=' not in after['prompt']
            assert 'delegate to ' in after['prompt'].lower()
    assert provenance['gate_relaxed'] is False


def test_fixture_v7_uses_explicit_outer_a2a_args_and_imperative_context_request():
    import hashlib,json
    from pathlib import Path
    root=Path('configs/llm-ab')
    provenance=json.loads((root/'workflow-cases-v7.provenance.json').read_text())
    old_bytes=(root/provenance['parent_fixture_file']).read_bytes()
    new_bytes=(root/provenance['fixture_file']).read_bytes()
    assert hashlib.sha256(old_bytes).hexdigest()==provenance['parent_fixture_file_sha256']
    assert hashlib.sha256(new_bytes).hexdigest()==provenance['fixture_file_sha256']
    old,new=json.loads(old_bytes),json.loads(new_bytes)
    assert len(old)==len(new)==20
    for before,after in zip(old,new):
        assert before['id']==after['id']
        assert before['group']==after['group']
        assert before['expected']==after['expected']
        if after['group']=='context' or after['group']=='composite':
            assert 'Call get_chunk_by_id exactly once with arguments' in after['prompt']
        if after['group']!='missing_user':
            assert 'native ' in after['prompt']
            assert 'outer arguments' in after['prompt']
    assert provenance['gate_relaxed'] is False


def test_fixture_v8_restores_proven_delegate_grammar_and_uses_scalar_chunk_id():
    import hashlib,json
    from pathlib import Path
    root=Path('configs/llm-ab')
    provenance=json.loads((root/'workflow-cases-v8.provenance.json').read_text())
    old_bytes=(root/provenance['parent_fixture_file']).read_bytes()
    new_bytes=(root/provenance['fixture_file']).read_bytes()
    assert hashlib.sha256(old_bytes).hexdigest()==provenance['parent_fixture_file_sha256']
    assert hashlib.sha256(new_bytes).hexdigest()==provenance['fixture_file_sha256']
    old,new=json.loads(old_bytes),json.loads(new_bytes)
    assert len(old)==len(new)==20
    for before,after in zip(old,new):
        assert before['id']==after['id']
        assert before['group']==after['group']
        assert before['expected']==after['expected']
        if after['group']=='context' or after['group']=='composite':
            assert 'chunk_id=\"800080:review:rev_800080_02:0\"' in after['prompt']
            assert 'arguments {\"chunk_id\"' not in after['prompt']
        if after['group']!='missing_user':
            assert 'delegate to ' in after['prompt'].lower()
    assert provenance['gate_relaxed'] is False


def test_fixture_v9_pins_the_exact_context_request_without_relaxing_gates():
    import hashlib,json
    from pathlib import Path
    root=Path('configs/llm-ab')
    provenance=json.loads((root/'workflow-cases-v9.provenance.json').read_text())
    old_bytes=(root/provenance['parent_fixture_file']).read_bytes()
    new_bytes=(root/provenance['fixture_file']).read_bytes()
    assert hashlib.sha256(old_bytes).hexdigest()==provenance['parent_fixture_file_sha256']
    assert hashlib.sha256(new_bytes).hexdigest()==provenance['fixture_file_sha256']
    old,new=json.loads(old_bytes),json.loads(new_bytes)
    assert len(old)==len(new)==20
    for before,after in zip(old,new):
        assert before['id']==after['id']
        assert before['group']==after['group']
        assert before['expected']==after['expected']
        if after['group'] in {'context','composite'}:
            assert 'chunk_id=\\"800080:review:rev_800080_02:0\\"' not in after['prompt']
            assert 'Return the complete function response unchanged' not in after['prompt']
            assert 'chunk_id="800080:review:rev_800080_02:0"' in after['prompt']
    assert provenance['validated_baseline_release'].startswith('499efed3')
    assert provenance['gate_relaxed'] is False


def test_fixture_v10_removes_nested_json_strings_without_relaxing_gates():
    import hashlib,json
    from pathlib import Path
    root=Path('configs/llm-ab')
    provenance=json.loads((root/'workflow-cases-v10.provenance.json').read_text())
    old_bytes=(root/provenance['parent_fixture_file']).read_bytes()
    new_bytes=(root/provenance['fixture_file']).read_bytes()
    assert hashlib.sha256(old_bytes).hexdigest()==provenance['parent_fixture_file_sha256']
    assert hashlib.sha256(new_bytes).hexdigest()==provenance['fixture_file_sha256']
    old,new=json.loads(old_bytes),json.loads(new_bytes)
    assert len(old)==len(new)==20
    for before,after in zip(old,new):
        assert before['id']==after['id']
        assert before['group']==after['group']
        assert before['expected']==after['expected']
        if after['group'] in {'recommendation','limits','empty_result'}:
            assert '[RECOMMENDATION_REQUEST]' in after['prompt']
            assert 'candidate_item_ids=' in after['prompt']
            assert '{"user_id"' not in after['prompt']
        if after['group'] in {'context','composite'}:
            assert '[CONTEXT_REQUEST]' in after['prompt']
            assert 'chunk_id=800080:review:rev_800080_02:0' in after['prompt']
        if after['group']=='composite':
            assert '[RECOMMENDATION_REQUEST]' in after['prompt']
    assert provenance['root_conversations']==20
    assert provenance['gate_relaxed'] is False


def test_fixture_v11_uses_role_lines_and_live_chunk_without_relaxing_gates():
    import hashlib,json
    from pathlib import Path
    root=Path('configs/llm-ab')
    provenance=json.loads((root/'workflow-cases-v11.provenance.json').read_text())
    old_bytes=(root/provenance['parent_fixture_file']).read_bytes()
    new_bytes=(root/provenance['fixture_file']).read_bytes()
    assert hashlib.sha256(old_bytes).hexdigest()==provenance['parent_fixture_file_sha256']
    assert hashlib.sha256(new_bytes).hexdigest()==provenance['fixture_file_sha256']
    old,new=json.loads(old_bytes),json.loads(new_bytes)
    assert len(old)==len(new)==provenance['root_conversations']==20
    assert [c['id'] for c in old]==[c['id'] for c in new]
    assert [c['group'] for c in old]==[c['group'] for c in new]
    for before,after in zip(old,new):
        assert before['expected'].get('trajectory')==after['expected'].get('trajectory')
        if after['group'] in {'recommendation','limits','empty'}:
            assert after['prompt'].startswith('MODE=SINGLE_RECOMMENDATION\nRECOMMENDATION_REQUEST=')
            assert '{"user_id"' not in after['prompt']
        if after['group']=='context':
            assert after['prompt'].startswith('MODE=SINGLE_CONTEXT\nCONTEXT_REQUEST=')
        if after['group']=='composite':
            assert after['prompt'].startswith('MODE=COMPOSITE\nRECOMMENDATION_REQUEST=')
            assert '\nCONTEXT_REQUEST=' in after['prompt']
        if after['group'] in {'context','composite'}:
            assert "chunk_id='800005:product_overview:overview:0'" in after['prompt']
            assert after['expected']['context']['arguments']['chunk_id']==provenance['attested_chunk_id']
        if after['group']=='missing_user':
            assert after['prompt'].startswith('MODE=MISSING_USER\nREQUEST=')
    assert provenance['rag_pipeline_run_id']=='rag-20260907T193000'
    assert provenance['gate_relaxed'] is False
