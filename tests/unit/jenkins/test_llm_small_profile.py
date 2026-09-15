from copy import deepcopy
import json
from pathlib import Path

import pytest

from tests.unit.jenkins.test_llm_agent_cd import champion
from jenkins.python.llm_agent_cd.release import release, digest
from jenkins.python.llm_agent_cd.manifests import backend_resources
from jenkins.python.llm_agent_cd.serving_profiles import validate_profile
from jenkins.python.llm_agent_cd.workflow_snapshot import snapshot, semantic_agent


def small():
    return json.loads(Path("configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m.json").read_text())


def qwen35_native():
    return json.loads(Path("configs/llm-ab/catalog/qwen3.5-0.8b-q4_0-native-tools-v1.json").read_text())


def qwen25_terminal():
    return json.loads(Path(
        "configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m-tools-v3.json"
    ).read_text())


def qwen35_native_v2():
    return json.loads(Path("configs/llm-ab/catalog/qwen3.5-0.8b-q4_0-native-tools-v2.json").read_text())


def qwen35_native_v3():
    return json.loads(Path("configs/llm-ab/catalog/qwen3.5-0.8b-q4_0-native-tools-v3.json").read_text())


def qwen35_b8646_stock():
    return json.loads(Path("configs/llm-ab/catalog/qwen3.5-0.8b-q4_0-b8646-stock-adk-v1.json").read_text())


def qwen35_b8646_stock_cache():
    return json.loads(Path(
        "configs/llm-ab/catalog/qwen3.5-0.8b-q4_0-b8646-stock-adk-cache-v2.json"
    ).read_text())


def test_small_manifest_and_legacy_identity(champion):
    old = deepcopy(champion)
    assert release(champion) == old
    llm = small()
    raw = {k: deepcopy(champion[k]) for k in ("config", "llm", "agent", "binding")}
    raw["llm"] = llm
    raw["binding"].update(managed_backend=True, backend_url="http://rec-llm-" + digest(llm)[:20] + ".kagent.svc.cluster.local:8000/v1")
    candidate = release(raw)
    container = backend_resources(candidate, "kagent", "downloader")[0]["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"] == {"cpu": "1", "memory": "1536Mi"}
    assert "--jinja" in container["args"]
    assert "--reasoning-budget" not in container["args"]
    assert candidate["config_id"] == champion["config_id"]
    assert candidate["llm_version_id"] != champion["llm_version_id"]


@pytest.mark.parametrize("field,value", [("resourceProfile", "arbitrary"), ("threads", 1), ("reasoningBudget", 256)])
def test_profile_rejects_mutable_settings(field, value):
    llm = small()
    llm["serving"][field] = value
    with pytest.raises(ValueError):
        validate_profile(llm)


@pytest.mark.parametrize("drift", [False, True])
def test_template_resume_verifies_immutable_data(monkeypatch, drift):
    from jenkins.python.llm_agent_cd import driver as module
    obj = {"kind": "ConfigMap", "metadata": {"name": "template", "labels": {"recsys.ai/owner": "llm-agent-cd"}},
           "immutable": True, "data": {"template.jinja": "pinned"}}
    live = deepcopy(obj)
    if drift:
        live["data"]["template.jinja"] = "changed"
    monkeypatch.setattr(module, "backend_resources", lambda *a: [obj])
    monkeypatch.setattr(module, "resources", lambda *a: [])
    d = object.__new__(module.Driver)
    d.namespace, d.image, d.secret = "kagent", "image", "secret"
    calls = []
    def kube(*args, **kwargs):
        calls.append(args)
        assert args[0] == "get", "resume must not overwrite a ConfigMap"
        return json.dumps(live)
    d.kube = kube
    if drift:
        with pytest.raises(ValueError, match="template drift"):
            d.deploy({})
    else:
        d.deploy({})
    assert len(calls) == 1


@pytest.mark.parametrize('drift',[False,True])
def test_paused_backend_resume_only_changes_replicas_after_identity_checks(monkeypatch,drift):
    from jenkins.python.llm_agent_cd import driver as module
    obj={'kind':'Deployment','metadata':{'name':'rec-llm-test','namespace':'kagent',
        'labels':{'recsys.ai/owner':'llm-agent-cd'},'annotations':{'recsys.ai/immutable-spec':'frozen'}},
        'spec':{'replicas':1,'template':{'spec':{'containers':[{'image':'pinned'}]}}}}
    live=deepcopy(obj);live['spec']['replicas']=0;live['metadata']['resourceVersion']='37'
    if drift: live['spec']['template']['spec']['containers'][0]['image']='foreign'
    monkeypatch.setattr(module,'backend_resources',lambda *a:[obj])
    monkeypatch.setattr(module,'resources',lambda *a:[])
    d=object.__new__(module.Driver);d.namespace,d.image,d.secret='kagent','image','secret'
    mutations=[]
    def kube(*args,stdin=None):
        if args[0]=='get':return json.dumps(live)
        if args[0]=='patch':mutations.append(json.loads(stdin))
        else:assert args[:2]==('rollout','status')
        return ''
    d.kube=kube
    if drift:
        with pytest.raises(ValueError,match='drift'):d.deploy({})
        assert not mutations
    else:
        d.deploy({})
        assert mutations==[[{'op':'test','path':'/metadata/resourceVersion','value':'37'},
            {'op':'test','path':'/spec/replicas','value':0},{'op':'replace','path':'/spec/replicas','value':1}]]


def test_global_defaults_not_inferred_from_old_recommendation(champion):
    mc = {"model": "qwen", "apiKeySecret": "secret", "apiKeySecretKey": "key",
          "openAI": {"baseUrl": "http://baseline/v1", "apiFormat": "chatCompletions",
                     "temperature": "0.2", "maxTokens": 384, "seed": 42}}
    specs = {}
    for role in ("coordinator", "context", "recommendation"):
        agent = deepcopy(champion["agent"])
        agent["systemMessage"] += "\nRuntime model configuration revision: frozen-marker.\n"
        if role == "coordinator":
            agent["tools"] = [{"type": "Agent", "agent": {"name": "recsys-" + r + "-agent-sandbox"}} for r in ("context", "recommendation")]
        specs[role] = {"declarative": agent, "substrate": {"workerPoolRef": {"name": "pool"}},
                       "sandbox": {"network": {"allowedDomains": ["baseline"]}}}
    result = snapshot(champion, specs, {r: deepcopy(mc) for r in specs}, mc)
    assert result["global_generation"]["temperature"] == "0.2"
    assert all(not o for o in result["agent_overrides"].values())
    assert "frozen-marker" in result["agents"]["recommendation"]["systemMessage"]


def test_marker_comparison_does_not_hide_business_prompt_changes():
    a = {"systemMessage": "Keep ranking.\nRuntime model configuration revision: a.\n"}
    b = {"systemMessage": "Keep ranking.\nRuntime model configuration revision: b.\n"}
    assert semantic_agent(a) == semantic_agent(b)
    b["systemMessage"] = b["systemMessage"].replace("Keep", "Change")
    assert semantic_agent(a) != semantic_agent(b)


def test_legacy_backend_rendering_is_not_reprofiled(champion):
    raw = {k: deepcopy(champion[k]) for k in ("config", "llm", "agent", "binding")}
    raw["binding"].update(managed_backend=True, backend_url="http://rec-llm-" + champion["llm_version_id"][:20] + ".kagent.svc.cluster.local:8000/v1")
    old = release(raw)
    assert old["llm_version_id"] == champion["llm_version_id"]
    container = backend_resources(old, "kagent", "downloader")[0]["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"] == {"requests": {"cpu": "2", "memory": "3Gi"}, "limits": {"cpu": "2", "memory": "5Gi"}}
    assert "--jinja" not in container["args"]
    assert "--reasoning-budget" in container["args"]


def test_profile_rejects_other_artifact():
    llm = small()
    llm["artifact_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="attested"):
        validate_profile(llm)


def test_template_fix_is_new_identity_without_mutating_v1(champion):
    from jenkins.python.llm_agent_cd.serving_profiles import tool_template, TEMPLATE_SHA256
    from hashlib import sha256
    llm = json.loads(Path("configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m-tools-v2.json").read_text())
    validate_profile(llm)
    assert digest(llm) != digest(small())
    assert sha256(tool_template().encode()).hexdigest() == TEMPLATE_SHA256
    assert '{{\\"name\\": <function-name>' not in tool_template()
    raw = {k: deepcopy(champion[k]) for k in ("config", "llm", "agent", "binding")}
    raw["llm"] = llm
    raw["binding"].update(managed_backend=True, backend_url="http://rec-llm-" + digest(llm)[:20] + ".kagent.svc.cluster.local:8000/v1")
    objects = backend_resources(release(raw), "kagent", "downloader")
    pod = objects[0]["spec"]["template"]["spec"]
    assert pod['nodeSelector']=={'recsys.ai/pool':'cpu-services'}
    assert pod['initContainers'][0]['image'].endswith('sha256:918bcd199c8c89f0ab2c667d065506a00cd6178d85052d4511ae26de8ec13803')
    assert "--chat-template-file" in pod["containers"][0]["args"]
    cm = next(o for o in objects if o["kind"] == "ConfigMap")
    assert cm["immutable"] is True
    assert cm["data"]["template.jinja"] == tool_template()
    assert any(v.get("configMap", {}).get("name") == cm["metadata"]["name"] for v in pod["volumes"])
    assert "chatTemplateSha256" not in small()["serving"]
    llm["serving"]["chatTemplateSha256"] = "0" * 64
    with pytest.raises(ValueError):
        validate_profile(llm)


def test_b8646_small_profile_is_allowed_for_operator_catalog_registration():
    from jenkins.python.llm_agent_cd.workflow_provision import acceptance_serving_profile

    assert acceptance_serving_profile("qwen25-small-cpu-b8646-v3")
    assert acceptance_serving_profile("qwen25-small-cpu-b8646-terminal-v4")
    assert not acceptance_serving_profile("operator-injected-profile")


def test_b8646_terminal_template_is_new_attested_llm_identity(champion):
    from hashlib import sha256
    from jenkins.python.llm_agent_cd.serving_profiles import (
        terminal_tool_template, TERMINAL_TEMPLATE_SHA256)

    llm = qwen25_terminal()
    old = json.loads(Path(
        "configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m-tools-v2.json"
    ).read_text())
    validate_profile(llm)
    assert digest(llm) != digest(old)
    assert sha256(terminal_tool_template().encode()).hexdigest() == (
        TERMINAL_TEMPLATE_SHA256)
    assert "<terminal_instruction>" in terminal_tool_template()
    assert 'reply exactly {"done":true}' in terminal_tool_template()
    raw = {k: deepcopy(champion[k]) for k in ("config", "llm", "agent", "binding")}
    raw["llm"] = llm
    raw["binding"].update(
        managed_backend=True,
        backend_url="http://rec-llm-" + digest(llm)[:20]
        + ".kagent.svc.cluster.local:8000/v1",
    )
    objects = backend_resources(release(raw), "kagent", "downloader")
    cm = next(o for o in objects if o["kind"] == "ConfigMap")
    args = objects[0]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert cm["data"]["template.jinja"] == terminal_tool_template()
    assert "--chat-template-file" in args
    assert "--reasoning-budget" not in args


def test_qwen35_native_profile_pins_official_tool_template_without_reasoning_budget(champion):
    from hashlib import sha256
    from jenkins.python.llm_agent_cd.serving_profiles import (
        qwen35_tool_template, QWEN35_TEMPLATE_SHA256, QWEN35_TEMPLATE_REVISION)
    llm = qwen35_native()
    validate_profile(llm)
    assert sha256(qwen35_tool_template().encode()).hexdigest() == QWEN35_TEMPLATE_SHA256
    assert llm['serving']['chatTemplateSourceRevision'] == QWEN35_TEMPLATE_REVISION
    assert 'reasoningBudget' not in llm['serving']
    assert 'reasoningBudgetMessage' not in llm['serving']
    raw = {k: deepcopy(champion[k]) for k in ('config', 'llm', 'agent', 'binding')}
    raw['llm'] = llm
    raw['binding'].update(managed_backend=True,
        backend_url='http://rec-llm-' + digest(llm)[:20] + '.kagent.svc.cluster.local:8000/v1')
    candidate = release(raw)
    objects = backend_resources(candidate, 'kagent', 'downloader')
    pod = objects[0]['spec']['template']['spec']
    args = pod['containers'][0]['args']
    assert pod['nodeSelector'] == {'recsys.ai/pool': 'ml-system'}
    assert pod['tolerations'] == [{
        'key': 'recsys.ai/workload', 'operator': 'Equal',
        'value': 'ml-system', 'effect': 'NoSchedule'}]
    assert '--jinja' in args and '--chat-template-file' in args
    assert '--reasoning-budget' not in args
    assert '--reasoning-budget-message' not in args
    assert pod['containers'][0]['resources']['requests'] == {'cpu': '100m', 'memory': '1536Mi'}
    cm = next(o for o in objects if o['kind'] == 'ConfigMap')
    assert cm['immutable'] is True
    assert cm['data']['template.jinja'] == qwen35_tool_template()


def test_qwen35_native_v2_disables_reasoning_without_budget(champion):
    llm = qwen35_native_v2()
    validate_profile(llm)
    raw = {k: deepcopy(champion[k]) for k in ('config', 'llm', 'agent', 'binding')}
    raw['llm'] = llm
    raw['binding'].update(managed_backend=True,
        backend_url='http://rec-llm-' + digest(llm)[:20] + '.kagent.svc.cluster.local:8000/v1')
    objects = backend_resources(release(raw), 'kagent', 'downloader')
    pod = objects[0]['spec']['template']['spec']
    args = pod['containers'][0]['args']
    assert args[args.index('--reasoning') + 1] == 'off'
    assert '--reasoning-budget' not in args
    assert pod['nodeSelector'] == {'recsys.ai/pool': 'ml-system'}


def test_qwen35_native_v3_uses_attested_embedded_tool_template(champion):
    llm = qwen35_native_v3()
    validate_profile(llm)
    raw = {k: deepcopy(champion[k]) for k in ('config', 'llm', 'agent', 'binding')}
    raw['llm'] = llm
    raw['binding'].update(managed_backend=True,
        backend_url='http://rec-llm-' + digest(llm)[:20] + '.kagent.svc.cluster.local:8000/v1')
    objects = backend_resources(release(raw), 'kagent', 'downloader')
    pod = objects[0]['spec']['template']['spec']
    args = pod['containers'][0]['args']
    assert args[args.index('--reasoning') + 1] == 'off'
    assert '--reasoning-budget' not in args and '--chat-template-file' not in args
    assert pod['nodeSelector'] == {'recsys.ai/pool': 'ml-system'}
    assert not any(o['kind'] == 'ConfigMap' for o in objects)


def test_qwen35_b8646_profile_is_digest_pinned_and_has_no_reasoning_budget(champion):
    llm = qwen35_b8646_stock()
    validate_profile(llm)
    assert llm['serving']['llamaCppBuild'] == 'b8646'
    assert llm['serving']['llamaCppRevision'] == '0c58ba3365d2bc717b447b5d70e4d6be09ff3c40'
    raw = {k: deepcopy(champion[k]) for k in ('config', 'llm', 'agent', 'binding')}
    raw['llm'] = llm
    raw['binding'].update(managed_backend=True,
        backend_url='http://rec-llm-' + digest(llm)[:20] + '.kagent.svc.cluster.local:8000/v1')
    objects = backend_resources(release(raw), 'kagent', 'downloader')
    pod = objects[0]['spec']['template']['spec']
    container = pod['containers'][0]
    assert container['image'] == llm['image'] and '@sha256:' in container['image']
    assert container['args'][container['args'].index('--reasoning') + 1] == 'off'
    assert '--jinja' in container['args'] and '--reasoning-budget' not in container['args']
    assert '--chat-template-file' not in container['args']
    assert pod['nodeSelector'] == {'recsys.ai/pool': 'ml-system'}


def test_qwen35_b8646_cache_profile_is_new_identity_and_bounds_official_cache(champion):
    old, llm = qwen35_b8646_stock(), qwen35_b8646_stock_cache()
    validate_profile(llm)
    assert digest(old) != digest(llm)
    assert llm['serving']['cacheRamMiB'] == 256
    raw = {k: deepcopy(champion[k]) for k in ('config', 'llm', 'agent', 'binding')}
    raw['llm'] = llm
    raw['binding'].update(managed_backend=True,
        backend_url='http://rec-llm-' + digest(llm)[:20] + '.kagent.svc.cluster.local:8000/v1')
    pod = backend_resources(release(raw), 'kagent', 'downloader')[0]['spec']['template']['spec']
    args = pod['containers'][0]['args']
    assert args[args.index('--cache-ram') + 1] == '256'
    assert args[args.index('--reasoning') + 1] == 'off'
    assert '--reasoning-budget' not in args and '--chat-template-file' not in args
    assert pod['nodeSelector'] == {'recsys.ai/pool': 'cpu-services'}


def test_runtime_props_must_attest_selected_tool_template():
    from jenkins.python.llm_agent_cd.driver import verify_tool_template_props
    from jenkins.python.llm_agent_cd.serving_profiles import (qwen35_tool_template,
        tool_template, QWEN35_TEMPLATE_SHA256, TEMPLATE_SHA256)
    serving = {'chatTemplateSha256': QWEN35_TEMPLATE_SHA256}
    caps = {'supports_tools': True, 'supports_tool_calls': True}
    # Prefer the dedicated tool-use template when the server exposes one.
    verify_tool_template_props({'chat_template_tool_use': qwen35_tool_template(),
                                'chat_template': 'wrong', 'chat_template_caps': caps}, serving)
    # An explicitly pinned --chat-template-file appears as the default template.
    verify_tool_template_props({'chat_template': qwen35_tool_template(),
                                'chat_template_caps': caps}, serving)
    # llama.cpp exposes a --chat-template-file without its trailing POSIX newline.
    verify_tool_template_props({'chat_template': tool_template().removesuffix('\n'),
                                'chat_template_caps': caps},
                               {'chatTemplateSha256': TEMPLATE_SHA256})
    with pytest.raises(ValueError, match='attestation'):
        verify_tool_template_props({'chat_template': 'wrong', 'chat_template_caps': caps}, serving)
    with pytest.raises(ValueError, match='capabilities'):
        verify_tool_template_props({'chat_template': qwen35_tool_template(),
                                    'chat_template_caps': {'supports_tools': False}}, serving)
