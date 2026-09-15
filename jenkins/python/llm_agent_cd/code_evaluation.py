"""Deterministic workflow scores. Never calls a model, tool or data backend.

Only collector-supplied task evidence is accepted. A missing observation is
UNKNOWN, not a false/zero value or evidence of successful execution.
"""
from copy import deepcopy
import json
import math
import re
from .release import digest
from .workflow import members
from .workflow_evidence import events, answer, inspect_workflow, own_usage, tool_value
from .evidence import task_of, recommendation
from .output_schema import validate as schema_valid, VERSION as SCHEMA_VERSION

VERSION = "workflow-code-v4"
HARD = ("trajectory_match", "release_consistency", "tool_arguments_match",
        "duplicate_tool_calls", "missing_user_safe", "schema_valid",
        "ranking_preserved", "empty_result_correct", "citation_validity",
        "functional_success")


def score(name, value=None, *, status=None, reason=""):
    if status is None:
        status = "PASS" if (value is True or (name == "duplicate_tool_calls" and value == 0)) else "FAIL"
    return {"name": name, "value": value, "status": status, "reason": reason,
            "required": name in HARD}


def parsed_answer(task):
    text = answer(task)
    if not text:
        raise ValueError("missing final output")
    return json.loads(text)


def citation_ids(value):
    if isinstance(value, dict):
        found = {str(value['chunk_id'])} if value.get('chunk_id') is not None else set()
        for key, child in value.items():
            if key in {'citation_ids', 'chunk_ids'} and isinstance(child, list):
                found.update(str(x) for x in child)
            else:
                found.update(citation_ids(child))
        return found
    if isinstance(value, list):
        return set().union(*(citation_ids(x) for x in value))
    return set()


def evaluate(request):
    """Input is an immutable DB snapshot, never a Langfuse client submission."""
    expected, workflow = request["expected"], request["workflow"]
    body, children = request["body"], request.get("children", {})
    result = {n: score(n, status="UNKNOWN", reason="required evidence missing") for n in HARD}
    def setscore(name, value=None, **kw):
        result[name] = score(name, value, **kw)
    def na(name): setscore(name, status="NOT_APPLICABLE", reason="not required by this fixture")
    if not expected.get("missing_user"): na("missing_user_safe")
    if not expected.get("recommendation"): na("ranking_preserved"); na("empty_result_correct")
    elif not expected["recommendation"].get("empty"): na("empty_result_correct")
    citation_required = expected.get('context', {}).get('tool') in {'build_user_rag_context', 'retrieve_rag_context', 'get_chunk_by_id'}
    if not citation_required and not expected.get("required_citation_ids"): na("citation_validity")
    task = task_of(body)
    runtime_rendered = (
        task.get("metadata", {}).get("runtime_rendered") is True
        and task.get("metadata", {}).get("runtime_output_profile")
        == "trusted-child-tool-results-v2"
    )
    try:
        rendered = parsed_answer(task)
        setscore("schema_valid", schema_valid(rendered,expected), reason=SCHEMA_VERSION)
    except (ValueError, TypeError):
        rendered = None
        if answer(task): setscore("schema_valid", False, reason="final output is not JSON")
    try:
        calls = events(task, "function_call")
        responses = events(task, "function_response")
        from .manifests import name as resource_name
        mapping = {"kagent__NS__" + resource_name(m).replace("-", "_"): role
                   for role, m in members(workflow).items()}
        actual = [mapping.get(c.get("name"), "foreign") for c in calls]
        complete = task.get("status", {}).get("state") in {"completed", "TASK_STATE_COMPLETED"}
        if complete:
            setscore("trajectory_match", actual == expected.get("trajectory", []))
        if expected.get("missing_user"):
            if rendered is not None:
                setscore("missing_user_safe", complete and not calls and isinstance(rendered, dict)
                         and rendered.get("clarification") == "Please provide user_id.")
            na("tool_arguments_match")
        summaries = [task]
        duplicate = max(0, len(calls) - len(set(actual)))
        arguments_ok, args_known = bool(actual), True
        source_ranked = None
        for call, role in zip(calls, actual):
            response = next((r for r in responses if r.get("id") == call.get("id")), None)
            if response is None:
                args_known = False; continue
            payload = response.get("response", {})
            if isinstance(payload, dict) and payload.get("subagent_session_id"):
                payload = children.get(payload["subagent_session_id"], {})
            child = task_of(payload)
            if not child.get("status"):
                args_known = False; continue
            summaries.append(child)
            cc, rr = events(child, "function_call"), events(child, "function_response")
            duplicate += max(0, len(cc) - 1)
            target = deepcopy(expected.get(role, {}))
            if role == "context" and target.get("arguments", {}).get("candidate_item_ids") == "$recommendation.items":
                if source_ranked is None:
                    args_known = False; continue
                target["arguments"]["candidate_item_ids"] = [x["item_id"] for x in source_ranked["items"]]
            tool_name = "get_personalized_recommendations" if role == "recommendation" else target.get("tool")
            arguments_ok &= len(cc) == 1 and cc[0].get("name") == tool_name and cc[0].get("args") == target.get("arguments")
            if role == "recommendation" and len(rr) == 1:
                source_ranked = recommendation(rr[0].get("response"))
                if runtime_rendered:
                    shown = source_ranked
                else:
                    try: shown = recommendation(parsed_answer(child))
                    except (ValueError, TypeError): shown = None
                if source_ranked is not None and shown is not None:
                    final = rendered.get(role) if len(actual)>1 and isinstance(rendered, dict) else rendered
                    setscore("ranking_preserved", shown == source_ranked and recommendation(final) == source_ranked)
                    if target.get("empty"):
                        setscore("empty_result_correct", source_ranked["items"] == [] and shown == source_ranked and len(cc) == 1)
            if role == 'context' and citation_required and len(rr) == 1:
                try:
                    final = rendered.get(role) if len(actual)>1 and isinstance(rendered,dict) else rendered
                    shown_ids = citation_ids(final if runtime_rendered else parsed_answer(child))
                    retrieved_ids = citation_ids(tool_value(rr[0].get('response')))
                    required_ids = set(map(str, expected.get('required_citation_ids', [])))
                    setscore('citation_validity', shown_ids <= retrieved_ids and required_ids <= shown_ids
                             and (bool(shown_ids) or not retrieved_ids))
                except (ValueError, TypeError):
                    pass
        if args_known and not expected.get("missing_user"):
            setscore("tool_arguments_match", arguments_ok)
        if complete and len(summaries) == len(actual) + 1:
            setscore("duplicate_tool_calls", duplicate)
        # The router pins the root adapter by release ID and the only legal
        # Coordinator tools are the two release-specific A2A names. Trusted
        # child task evidence therefore proves the whole chain stayed inside
        # the assigned immutable workflow without runtime-injected metadata.
        if complete and len(summaries) == len(actual) + 1:
            setscore("release_consistency", all(role != "foreign" for role in actual))
        check = inspect_workflow(body, expected, workflow, request.get("message_id"), child_reader=lambda sid: children[sid])
        setscore("functional_success", check["verdict"] == "PASS" if check["verdict"] != "HOLD" else None,
                 status=check["verdict"] if check["verdict"] != "HOLD" else "UNKNOWN", reason=check["reason"])
        usage = [own_usage(t) for t in summaries]
        for name in ("input_tokens", "output_tokens"):
            known = len(summaries) == len(actual)+1 and all(name in u for u in usage)
            setscore(name, sum(u[name] for u in usage) if known else None,
                     status="OBSERVED" if known else "UNAVAILABLE", reason="sum own LLM calls once")
    except (ValueError, KeyError, TypeError, IndexError):
        # Retain prior known failures. Never manufacture zero calls from a
        # malformed or incomplete task snapshot.
        pass
    setscore("retrieval_recall_at_k", status="UNAVAILABLE", reason="no reviewed relevance labels in v1 fixture")
    duration = request.get("duration_seconds")
    valid_duration = type(duration) in {int, float} and math.isfinite(duration) and duration >= 0
    setscore("root_latency_seconds", duration if valid_duration else None,
             status="OBSERVED" if valid_duration else "UNAVAILABLE")
    required = [s for s in result.values() if s["required"] and s["status"] != "NOT_APPLICABLE"]
    verdict = "FAIL" if any(s["status"] == "FAIL" for s in required) else "HOLD" if any(s["status"] != "PASS" for s in required) else "PASS"
    return {"evaluator_version": VERSION, "evidence_checksum": digest(request),
            "verdict": verdict, "scores": list(result.values())}


def score_payloads(evaluation, metadata):
    """Serialize only allowlisted metadata; never export task/tool payloads."""
    if not re.fullmatch(r'[0-9a-f]{32}', metadata.get('trace_id', '')) or metadata['trace_id'] == '0' * 32:
        raise ValueError('missing valid trace identity')
    fields = ("experiment_id", "variant", "release_id", "config_id", "llm_version_id", "source", "fixture_checksum")
    common = {k: metadata[k] for k in fields if k in metadata}
    version = evaluation['evaluator_version']
    common.update(evaluator_version=version, evidence_checksum=evaluation["evidence_checksum"])
    payloads = []
    for s in evaluation["scores"]:
        observed = s["status"] in {"PASS", "FAIL", "OBSERVED"} and s["value"] is not None
        value = s["value"] if observed else s["status"]
        kind = "BOOLEAN" if type(value) is bool else "NUMERIC" if type(value) in {int, float} else "CATEGORICAL"
        # Scores API represents BOOLEAN scores as numeric 0/1, not JSON bool.
        if kind == "BOOLEAN": value = int(value)
        payloads.append({"id": digest([metadata["experiment_id"], metadata["request_key"], version, s["name"]]),
                         "traceId": metadata["trace_id"], "name": s["name"], "value": value,
                         "dataType": kind, "metadata": {**common, "status": s["status"], "required": s["required"]}})
        if metadata.get('source')=='offline':
            payloads[-1]['observationId'] = digest([metadata['trace_id'],'root'])[:16]
    return payloads
