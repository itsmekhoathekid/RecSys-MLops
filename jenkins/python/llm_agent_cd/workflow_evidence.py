"""Fail-closed workflow assertions. Missing child evidence is never a PASS.

Nested A2A tasks are accepted only from the trusted adapter response, not from
user messages or model-authored JSON text. Runtime-specific child trace export
must populate actual function responses before a workflow can pass.
"""
import json
import re
from .evidence import task_of, inspect as inspect_recommendation, recommendation, COMPLETED
from .workflow import members
from .manifests import name as resource_name


def events(task, kind):
    parts = [p for h in task.get("history", []) if h.get("role") not in {"user", "ROLE_USER"} for p in h.get("parts", [])]
    parts += [p for a in task.get("artifacts", []) for p in a.get("parts", [])]
    found = {}
    for p in parts:
        if p.get("metadata", {}).get("adk_type") == kind:
            d = p.get("data", {})
            key = d.get("id")
            if not key:
                raise ValueError("uncorrelated tool event")
            if key in found and found[key] != d:
                raise ValueError("conflicting tool event")
            found[key] = d
    return list(found.values())


def answer(task):
    return " ".join(p.get("text", "") for a in task.get("artifacts", []) for p in a.get("parts", [])).strip()


def business_json(value):
    """Remove the one FastMCP dict-return envelope used by the adapter.

    This is deliberately narrower than ``tool_value``: raw dependency
    evidence keeps every field, while a completed specialist's A2A result is
    compared with the same business value that the runtime adapter renders.
    """
    if isinstance(value, dict) and set(value) == {"output"} and isinstance(
            value["output"], dict):
        return value["output"]
    return value


def tool_value(value):
    """Normalize transport wrappers only, matching the shared runtime renderer."""
    if not isinstance(value, dict) or value.get('error') or value.get('isError') is True:
        raise ValueError('unusable tool result')
    wrapped = isinstance(value.get('structuredContent'), dict) or isinstance(value.get('content'), list)
    if isinstance(value.get('structuredContent'), dict):
        value = value['structuredContent']
    elif isinstance(value.get('result'), str):
        value = json.loads(value['result'])
    elif isinstance(value.get('content'), list):
        if len(value['content']) != 1 or not isinstance(value['content'][0].get('text'), str):
            raise ValueError('ambiguous tool result')
        value = json.loads(value['content'][0]['text'])
    if not isinstance(value, dict):
        raise ValueError('tool result must be a JSON object')
    # FastMCP's dict[str, object] return schema wraps the actual object in a
    # sole output key. Strip only that known transport envelope, never a field
    # on a raw business object or an object with additional metadata.
    if wrapped and set(value) == {'output'} and isinstance(value['output'],dict):
        value = value['output']
    return value


def own_usage(task):
    # Only usage attached to this agent's own LLM events, not aggregate remote
    # kagent_usage_metadata from delegated tasks (which would double count).
    samples = {}
    for a in task.get("artifacts", []):
        usage = a.get("metadata", {}).get("adk_usage_metadata")
        if not usage or not a.get("artifactId"):
            continue
        key = a["artifactId"]
        if key in samples and samples[key] != usage:
            return {}
        samples[key] = usage
    output = {}
    for key, wire in (("input_tokens", "promptTokenCount"), ("output_tokens", "candidatesTokenCount")):
        values = [s.get(wire) for s in samples.values()]
        if values and all(isinstance(v, (int, float)) and v >= 0 and float(v).is_integer() for v in values):
            output[key] = int(sum(values))
    return output


def inspect_workflow(body, expected, workflow, message_id=None, child_reader=None):
    def result(verdict, reason, **extra):
        return {"verdict": verdict, "reason": reason, "error": False,
                "contract_failure": verdict == "FAIL", **extra}
    if body.get("error"):
        return result("FAIL", "workflow A2A error", error=True)
    task = task_of(body)
    if task.get("status", {}).get("state") in {"failed", "TASK_STATE_FAILED", "rejected", "TASK_STATE_REJECTED"}:
        return result("FAIL", "workflow runtime failed", error=True, contract_failure=False)
    if task.get("status", {}).get("state") not in COMPLETED:
        return result("HOLD", "workflow incomplete")
    # Do not inspect old turns as if they belong to this invocation.
    history = task.get("history", [])
    if sum(h.get("role") in {"user", "ROLE_USER"} for h in history) > 1:
        return result("HOLD", "multi-turn evidence needs turn-scoped child trace")
    if not expected:
        return result("HOLD", "organic workflow requires outcome/trajectory evidence")
    try:
        calls, responses = events(task, "function_call"), events(task, "function_response")
    except ValueError as exc:
        return result("HOLD", str(exc))
    text = answer(task)
    runtime_rendered = (
        task.get("metadata", {}).get("runtime_rendered") is True
        and task.get("metadata", {}).get("runtime_output_profile")
        == "trusted-child-tool-results-v2"
    )
    if expected.get("missing_user"):
        if calls:
            return result("FAIL", "delegation/tool call without required user ID")
        return result("PASS", "requested user ID") if re.search(r"user.?id|mã người dùng", text, re.I) else result("HOLD", "missing-user response not verifiable")
    variants = members(workflow)
    lookup = {"kagent__NS__" + resource_name(member).replace("-", "_"): role
              for role, member in variants.items()}
    actual = [lookup.get(call.get("name"), "foreign") for call in calls]
    if actual != expected["trajectory"]:
        return result("FAIL" if calls else "HOLD", "specialist trajectory differs from expected release/order")
    if len(responses) != len(calls) or {c["id"] for c in calls} != {r["id"] for r in responses}:
        return result("HOLD", "incomplete child response correlation")
    children, child_outputs = [], {}
    for call, role in zip(calls, actual):
        response = next(r for r in responses if r["id"] == call["id"])
        payload = response.get("response", {})
        if isinstance(payload, dict) and payload.get("error"):
            return result("FAIL", role + " runtime failed", error=True, contract_failure=False)
        if isinstance(payload, dict) and payload.get("subagent_session_id"):
            if child_reader is None:
                return result("HOLD", "child session reader unavailable")
            try:
                payload = child_reader(payload["subagent_session_id"])
            except Exception:
                return result("HOLD", "missing trusted " + role + " child session")
        # Never parse an LLM's text into purported execution history.
        if not isinstance(payload, dict) or not isinstance(payload.get("result", {}), dict):
            return result("HOLD", "missing trusted " + role + " child task evidence")
        child = task_of(payload) if isinstance(payload, dict) else {}
        if child.get("status", {}).get("state") in {"failed", "TASK_STATE_FAILED", "rejected", "TASK_STATE_REJECTED"}:
            return result("FAIL", role + " runtime failed", error=True, contract_failure=False)
        if not child.get("artifacts") or not child.get("status"):
            return result("HOLD", "missing trusted " + role + " child task evidence")
        for tool in events(child, "function_response"):
            output = tool.get("response", {})
            if isinstance(output, dict) and (output.get("isError") is True or output.get("error")):
                return result("FAIL", role + " tool failed", error=True, contract_failure=False)
            try:
                value=tool_value(output)
                if value.get('partial') is True or value.get('errors'):
                    return result('FAIL',role+' dependency returned partial/error evidence',error=True,contract_failure=False)
            except (ValueError,TypeError):
                return result('HOLD','malformed dependency evidence')
        try:
            cc, rr = events(child, "function_call"), events(child, "function_response")
        except ValueError:
            return result("HOLD", "uncorrelated " + role + " tool evidence")
        source = None
        if role == "recommendation":
            if runtime_rendered:
                target = expected.get("recommendation") or {}
                if (len(cc) != 1 or cc[0].get("name") != "get_personalized_recommendations"
                        or cc[0].get("args") != target.get("arguments")
                        or len(rr) != 1 or rr[0].get("id") != cc[0].get("id")):
                    return result("FAIL", "recommendation tool contract violation")
                source = recommendation(rr[0].get("response"))
                if source is None:
                    return result("HOLD", "missing structured recommendation tool response")
                if (len(source.get("items", [])) > target.get("arguments", {}).get("top_k", 0)
                        or source.get("user_id") != target.get("arguments", {}).get("user_id")
                        or (target.get("empty") and source.get("items") != [])):
                    return result("FAIL", "recommendation tool result violates fixture")
                verdict = result("PASS", "ranked tool output used by trusted runtime renderer")
            else:
                verdict = inspect_recommendation(payload, expected.get("recommendation"))
        else:
            target = expected["context"]
            if len(cc) != 1 or cc[0].get("name") != target["tool"]:
                return result("FAIL", "context tool contract violation")
            arguments = dict(target["arguments"])
            if arguments.get("candidate_item_ids") == "$recommendation.items":
                ranked = recommendation(child_outputs.get("recommendation"))
                if ranked is None or any("item_id" not in i for i in ranked["items"]):
                    return result("HOLD", "missing preceding recommendation item IDs")
                arguments["candidate_item_ids"] = [i["item_id"] for i in ranked["items"]]
            if cc[0].get("args") != arguments or len(rr) != 1 or rr[0].get("id") != cc[0]["id"]:
                return result("FAIL", "context arguments/response mismatch")
            if child.get("status", {}).get("state") not in COMPLETED:
                return result("HOLD", "context not terminal")
            # Grounding requires structured output, not an unverified prose assertion.
            try:
                source = tool_value(rr[0].get("response"))
                if runtime_rendered:
                    rendered = source
                else:
                    rendered = json.loads(answer(child).removeprefix("```json").removesuffix("```").strip())
            except ValueError:
                return result("HOLD", "context final outcome not machine-verifiable")
            verdict = result("PASS", "grounded tool output used by trusted runtime renderer" if runtime_rendered
                             else "grounded tool output preserved") if rendered == source else result("FAIL", "context output changed tool evidence")
        children.append({"role": role, **verdict, **own_usage(child),
                         "tool_calls": len(events(child, "function_call"))})
        if verdict["verdict"] != "PASS":
            return result(verdict["verdict"], verdict["reason"], children=children)
        if runtime_rendered:
            child_outputs[role] = business_json(source)
        else:
            try:
                child_outputs[role] = business_json(json.loads(
                    answer(child).removeprefix("```json").removesuffix("```").strip()))
            except ValueError:
                return result("HOLD", "child outcome not structured", children=children)
    if not text:
        return result("HOLD", "missing workflow final outcome", children=children)
    # Do not assert that final output preserved ranking solely on child success.
    try:
        rendered = json.loads(text.removeprefix("```json").removesuffix("```").strip())
    except ValueError:
        return result("HOLD", "workflow final outcome not machine-verifiable", children=children)
    for role, source in child_outputs.items():
        final = rendered.get(role) if len(actual) > 1 and isinstance(rendered, dict) else rendered
        if final != source:
            return result("FAIL", "workflow final output changed " + role + " result", children=children)
    agents = [{"role": "coordinator", "verdict": "PASS", "error": False,
               "child_calls": len(calls), **own_usage(task)}, *children]
    usage = {key: sum(a[key] for a in agents) for key in ("input_tokens", "output_tokens")
             if all(key in a for a in agents)}
    return result("PASS", "workflow trajectory and outcomes verified", children=children, agents=agents,
                  child_calls=len(calls), tool_calls=sum(c["tool_calls"] for c in children), **usage)
