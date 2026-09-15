"""Immutable, role-based release identity for a complete A2A workflow.

Physical SandboxAgent names are rendered *after* hashing semantic templates.
Global defaults never update a live ModelConfig in place.
"""
from copy import deepcopy

from .release import digest, release

ROLES = ("coordinator", "context", "recommendation")
TOKENS = {role: "{{agent." + role + "}}" for role in ROLES[1:]}


def workflow(value):
    r = deepcopy(value)
    fields = {"schema_version", "scope", "global_generation", "agent_overrides",
              "llm_overrides", "change_scope",
              "llm", "agents", "bindings", "release_id", "workflow_release_id",
              "config_id", "llm_version_id", "binding", "agent", "config",
              "runtime"}
    if set(r) - fields or r.get("schema_version") != 1 or r.get("scope") != "workflow":
        raise ValueError("invalid workflow schema")
    if set(r["agents"]) != set(ROLES) or set(r["bindings"]) != set(ROLES):
        raise ValueError("workflow requires exactly three roles")
    if set(r.get("agent_overrides", {})) - set(ROLES):
        raise ValueError("unknown override role")
    if set(r.get("llm_overrides", {})) - set(ROLES):
        raise ValueError("unknown LLM override role")
    if r.get("change_scope", "workflow") not in {"workflow", "coordinator"}:
        raise ValueError("unsupported workflow change scope")
    overrides = {role: r.get("agent_overrides", {}).get(role, {}) for role in ROLES}
    llm_overrides = r.get("llm_overrides", {})
    effective_llms = {
        role: deepcopy(llm_overrides.get(role, r["llm"])) for role in ROLES
    }
    effective = {}
    for role in ROLES:
        member = release({"config": {**r["global_generation"], **overrides[role]},
                          "llm": effective_llms[role], "agent": r["agents"][role],
                          "binding": r["bindings"][role]})
        effective[role] = member["config"]
        if role != "coordinator" and any(t.get("type") == "Agent" for t in member["agent"]["tools"]):
            raise ValueError("specialists must not invoke other agents")
    coordinator_tools = r["agents"]["coordinator"]["tools"]
    if len(coordinator_tools) != 2 or any(t.get("type") != "Agent" for t in coordinator_tools):
        raise ValueError("Coordinator may expose only the two A2A specialist tools")
    refs = [t["agent"]["name"] for t in coordinator_tools]
    if sorted(refs) != sorted(TOKENS.values()):
        raise ValueError("Coordinator must bind exactly the two semantic specialist roles")
    runtime = r.get("runtime")
    if runtime is not None:
        required_runtime = {"go_adk_image"}
        optional_runtime = {"a2a_description_profile", "a2a_name_profile",
                            "deterministic_output", "specialist_terminal_output"}
        if (not required_runtime.issubset(runtime)
                or set(runtime) - required_runtime - optional_runtime):
            raise ValueError("invalid immutable workflow runtime identity")
        import re
        if not re.fullmatch(r".+/golang-adk@sha256:[0-9a-f]{64}",
                            runtime.get("go_adk_image", "")):
            raise ValueError("workflow Go ADK image must be digest pinned")
        if ("a2a_description_profile" in runtime
                and runtime["a2a_description_profile"] not in {
                    "role-v1", "role-terminal-v2"}):
            raise ValueError("unsupported A2A description profile")
        if ("a2a_name_profile" in runtime
                and runtime["a2a_name_profile"] != "role-v1"):
            raise ValueError("unsupported A2A name profile")
        if ("deterministic_output" in runtime
                and runtime["deterministic_output"] not in {
                    "a2a-results-v1", "trusted-child-tool-results-v2"}):
            raise ValueError("unsupported deterministic output profile")
        if ("specialist_terminal_output" in runtime
                and runtime["specialist_terminal_output"] != "done-marker-v1"):
            raise ValueError("unsupported specialist terminal output profile")
    ids = {"config_id": digest(effective), "llm_version_id": digest(r["llm"])}
    identity = {**ids, "agents": r["agents"], "scope": "workflow"}
    # Legacy releases intentionally keep their historical ID. New releases pin
    # the actual worker runtime so an ADK repair can never reuse a quarantined ID.
    if runtime is not None:
        identity["runtime"] = runtime
    # Optional per-role LLM bindings are identity-bearing. Legacy releases
    # omit these fields and therefore retain their historical release IDs.
    if "llm_overrides" in r:
        identity["llm_overrides"] = llm_overrides
    if "change_scope" in r:
        identity["change_scope"] = r["change_scope"]
    wid = digest(identity)
    ids.update(release_id=wid, workflow_release_id=wid)
    for key, expected in ids.items():
        if key in r and r[key] != expected:
            raise ValueError(key + " checksum mismatch")
    r.update(ids)
    r["agent_overrides"] = overrides
    # Explicit compatibility view for the existing durable routing state machine.
    r.update(binding=deepcopy(r["bindings"]["coordinator"]),
             agent=deepcopy(r["agents"]["coordinator"]), config=effective["coordinator"])
    return r


def role_llms(value):
    """Return immutable effective LLM identities for each workflow role."""
    r = workflow(value)
    overrides = r.get("llm_overrides", {})
    return {role: deepcopy(overrides.get(role, r["llm"])) for role in ROLES}


def prompt_checksum(value):
    """Hash the semantic prompt surface shared by both experiment arms.

    Physical release-specific agent names are rendered only after this hash is
    computed, so a Coordinator-only LLM experiment can prove that neither its
    business prompt nor either specialist prompt drifted between arms.
    """
    r = workflow(value)
    return digest({role: r["agents"][role]["systemMessage"] for role in ROLES})


def members(value):
    r = workflow(value)
    effective_llms = role_llms(r)
    names = {role: "rec-ab-" + digest([r["release_id"], role])[:20] for role in ROLES}
    names["coordinator"] = "rec-ab-" + r["release_id"][:20]
    if r.get("runtime", {}).get("a2a_name_profile") == "role-v1":
        for role in ROLES[1:]:
            names[role] = "rec-ab-" + role + "-" + digest(
                [r["release_id"], role])[:16]

    def render(v):
        if isinstance(v, dict):
            return {k: render(x) for k, x in v.items()}
        if isinstance(v, list):
            return [render(x) for x in v]
        if isinstance(v, str):
            for role, token in TOKENS.items():
                v = v.replace("kagent__NS__" + token, "kagent__NS__" + names[role].replace("-", "_"))
                v = v.replace(token, names[role])
        return v

    result = {}
    for role in ROLES:
        m = release({"config": {**r["global_generation"], **r["agent_overrides"][role]},
                     "llm": effective_llms[role], "agent": r["agents"][role],
                     "binding": r["bindings"][role]})
        m["agent"] = render(m["agent"])
        m["release_id"] = r["release_id"] if role == "coordinator" else digest([r["release_id"], role])
        m["workflow_release_id"] = r["release_id"]
        m["role"] = role
        m["resource_name"] = names[role]
        if r.get("runtime") is not None:
            m["runtime"] = deepcopy(r["runtime"])
        result[role] = m
    return result


def validate_workflow(a, b, mode):
    a, b = workflow(a), workflow(b)
    if a.get("runtime") != b.get("runtime"):
        raise ValueError("workflow runtime must remain fixed inside an experiment")
    expected = {"config_only": (True, False), "llm_only": (False, True), "combined": (True, True)}
    changed = (a["config_id"] != b["config_id"], a["llm_version_id"] != b["llm_version_id"])
    if changed == (False, False):
        raise ValueError("NOOP: no effective workflow change")
    if changed != expected.get(mode):
        raise ValueError("experiment mode does not match effective workflow diff")
    if a["agents"] != b["agents"] or a["agent_overrides"] != b["agent_overrides"]:
        raise ValueError("agent templates, tools and overrides must remain fixed")
    if b.get("change_scope", "workflow") == "coordinator":
        if mode != "llm_only":
            raise ValueError("coordinator-only scope currently supports llm_only")
        before, after = members(a), members(b)
        if before["coordinator"]["llm_version_id"] == after["coordinator"]["llm_version_id"]:
            raise ValueError("coordinator-only experiment must change Coordinator LLM")
        for role in ("context", "recommendation"):
            if (before[role]["llm_version_id"] != after[role]["llm_version_id"]
                    or before[role]["config_id"] != after[role]["config_id"]
                    or a["bindings"][role] != b["bindings"][role]):
                raise ValueError("coordinator-only experiment changed specialist " + role)
        if (a["global_generation"] != b["global_generation"]
                or a["config_id"] != b["config_id"]):
            raise ValueError("coordinator-only experiment changed generation config")
    if mode == "config_only" and a["bindings"] != b["bindings"]:
        raise ValueError("config_only must reuse all immutable backend bindings")


def diff(a, b):
    ma, mb = members(a), members(b)
    return {role: {"before": ma[role]["config"], "after": mb[role]["config"],
                   "overrides": b["agent_overrides"][role],
                   "changed": ma[role]["config_id"] != mb[role]["config_id"],
                   "llm_changed": ma[role]["llm_version_id"] != mb[role]["llm_version_id"]}
            for role in ROLES}
