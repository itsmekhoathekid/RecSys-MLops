"""Build an immutable workflow baseline from live specs and an attested LLM.

Reads no Secret values. Does not mutate Kubernetes, routes or champion state.
"""
import argparse
from copy import deepcopy
import json
import re
from pathlib import Path
from .driver import command
from .release import release, digest
from .state import StateStore
from .workflow import ROLES, TOKENS


def generation(mc):
    result = {k: deepcopy(v) for k, v in mc["openAI"].items()
              if k not in {"baseUrl", "apiFormat"}}
    result["temperature"] = str(float(result["temperature"]))
    return result


def semantic_agent(agent):
    """Ignore only the generated terminal marker when comparing live baselines.

    The actual marker remains intact in the immutable release and both branches.
    """
    result = deepcopy(agent)
    result["systemMessage"] = re.sub(
        r"\nRuntime model configuration revision: [^\n]+\.\s*\Z", "",
        result["systemMessage"]).rstrip()
    return result


def snapshot(recommendation, specs, model_configs, global_model_config):
    if recommendation.get("scope") == "workflow":
        raise ValueError("expected attested recommendation baseline")
    global_config = generation(global_model_config)
    agents, bindings, overrides = {}, {}, {}
    for role in ROLES:
        spec, mc = specs[role], model_configs[role]
        if mc["model"] != recommendation["binding"]["model_alias"] or mc["openAI"]["baseUrl"] != recommendation["binding"]["backend_url"]:
            raise ValueError("baseline members do not share the attested LLM binding")
        for wire, bound in (("apiKeySecret", "api_key_secret"), ("apiKeySecretKey", "api_key_secret_key")):
            if mc.get(wire) != recommendation["binding"].get(bound):
                raise ValueError("baseline credential reference differs from attested binding")
        if mc.get("defaultHeaders", {}) != recommendation["binding"].get("default_headers", {}):
            raise ValueError("baseline routing headers differ from attested binding")
        if any(mc["openAI"].get(k) != v for k, v in {"apiFormat": "chatCompletions"}.items() if k in mc["openAI"]):
            raise ValueError("unsupported API format")
        effective = generation(mc)
        # ModelConfig serializes decimal zero as "0" while release() canonicalizes
        # it to "0.0". Representation differences are not fixed agent overrides.
        effective["temperature"] = str(float(effective["temperature"]))
        overrides[role] = {k: v for k, v in effective.items() if k not in global_config or str(v) != str(global_config[k])}
        if set(global_config) - set(effective):
            raise ValueError("missing effective config cannot be silently inherited")
        agents[role] = {k: deepcopy(v) for k, v in spec["declarative"].items() if k in {"runtime", "systemMessage", "tools", "a2aConfig"}}
        bindings[role] = {**deepcopy(recommendation["binding"]), "worker_pool": spec["substrate"]["workerPoolRef"]["name"],
                          "allowed_domains": deepcopy(spec["sandbox"]["network"]["allowedDomains"])}
        if role == "coordinator":
            if len(agents[role]["tools"]) != 2 or any(
                    tool.get("type") != "Agent" for tool in agents[role]["tools"]):
                raise ValueError("Coordinator baseline must expose only two A2A specialist tools")
            seen = set()
            for tool in agents[role]["tools"]:
                if tool.get("type") != "Agent":
                    continue
                old = tool["agent"]["name"]
                target = next((r for r in TOKENS if r in old), None)
                if target is None or target in seen:
                    raise ValueError("unexpected Coordinator agent graph")
                seen.add(target)
                tool["agent"]["name"] = TOKENS[target]
                agents[role]["systemMessage"] = agents[role]["systemMessage"].replace("kagent__NS__" + old.replace("-", "_"), "kagent__NS__" + TOKENS[target])
            if seen != set(TOKENS):
                raise ValueError("Coordinator must delegate to both specialists")
    return release({"schema_version": 1, "scope": "workflow", "global_generation": global_config,
                    "agent_overrides": overrides, "llm": recommendation["llm"], "agents": agents, "bindings": bindings})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--recommendation-state", default="s3://recsys-llm-ab/recommendation/state.json")
    p.add_argument("--namespace", default="kagent")
    p.add_argument("--output", required=True)
    p.add_argument("--global-model-config", default="recsys-global-model-config")
    args = p.parse_args()
    store = StateStore(args.recommendation_state)
    state, etag = store.read()
    names = {"coordinator": "recsys-coordinator-agent-sandbox", "context": "recsys-context-agent-sandbox",
             "recommendation": "recsys-recommendation-agent-sandbox"}
    captured = {}

    def get(kind, name):
        obj = json.loads(command("kubectl", "-n", args.namespace, "get", kind, name, "-o", "json"))
        previous = captured.get((kind, name))
        if previous and any(previous["metadata"][k] != obj["metadata"][k] for k in ("uid", "resourceVersion")):
            raise ValueError("STALE_SNAPSHOT: " + kind + "/" + name)
        captured[(kind, name)] = obj
        return obj

    helm = json.loads(command("helm", "status", "recsys-global-model-config", "-n", args.namespace, "-o", "json"))
    global_obj = get("modelconfig", args.global_model_config)
    specs, configs = {}, {}
    for role, name in names.items():
        specs[role] = get("sandboxagent", name)["spec"]
        configs[role] = get("modelconfig", specs[role]["declarative"]["modelConfig"])["spec"]
    result = snapshot(state["champion"], specs, configs, global_obj["spec"])
    # The active Recommendation path must agree with the default agent before
    # replacing its router with a directly pinned workflow specialist.
    if result["config"] != state["champion"]["config"] or generation(configs["recommendation"]) != state["champion"]["config"]:
        raise ValueError("HOLD: default and active Recommendation generation differ")
    if semantic_agent(result["agents"]["recommendation"]) != semantic_agent(state["champion"]["agent"]):
        raise ValueError("HOLD: default and active Recommendation prompt/tools differ")
    # Re-read all objects, not merely the shared ModelConfig, before publishing.
    for (kind, name), before in list(captured.items()):
        after = json.loads(command("kubectl", "-n", args.namespace, "get", kind, name, "-o", "json"))
        if any(before["metadata"][k] != after["metadata"][k] for k in ("uid", "resourceVersion")):
            raise ValueError("STALE_SNAPSHOT: " + kind + "/" + name)
    _, current_etag = store.read()
    if current_etag != etag:
        raise ValueError("STALE_SNAPSHOT: recommendation state changed")
    helm_after = json.loads(command("helm", "status", "recsys-global-model-config", "-n", args.namespace, "-o", "json"))
    if helm["version"] != helm_after["version"] or helm_after["info"]["status"] != "deployed":
        raise ValueError("STALE_SNAPSHOT: global Helm release changed or not deployed")
    provenance = {"recommendation_state_etag": etag, "llm_version_id": result["llm_version_id"],
                  "global_helm_revision": helm["version"],
                  "objects": [{"kind": k, "name": n, "uid": o["metadata"]["uid"],
                               "resourceVersion": o["metadata"]["resourceVersion"],
                               "spec_checksum": digest(o["spec"])} for (k, n), o in captured.items()]}
    # Create-only output prevents accidentally overwriting a previous baseline.
    with Path(args.output).open("x") as output:
        output.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    with Path(args.output + ".provenance.json").open("x") as output:
        output.write(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    print(result["release_id"])

if __name__ == "__main__":
    main()
