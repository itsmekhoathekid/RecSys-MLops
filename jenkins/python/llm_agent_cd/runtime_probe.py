"""Create-only stock ADK compatibility probes; requests are never replayed."""
import json
import os
import re
import signal
import time
import uuid
from contextlib import contextmanager

import httpx
from botocore.exceptions import ClientError

from .evidence import task_of, COMPLETED
from .release import digest
from .workflow import members
from .workflow_evidence import answer, business_json, events
from .manifests import name as resource_name

REQUEST_TIMEOUT_SECONDS = 600
CANDIDATE_A2A_TIMEOUT_SECONDS = 120
ADAPTER_READY_TIMEOUT_SECONDS = 60


class WallClockDeadline(TimeoutError):
    pass


@contextmanager
def wall_clock_deadline(seconds):
    """Enforce elapsed time even when the HTTP peer emits heartbeat bytes."""
    def expired(_signum, _frame):
        raise WallClockDeadline("create-only A2A probe exceeded wall-clock budget")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def stock_probe_verdict(body, expected_name, expected_args, *, require_final=False,
                        terminal_marker=False):
    """Require one native call/response and optionally exact JSON rendering."""
    try:
        task = task_of(body)
        calls = events(task, "function_call")
        responses = events(task, "function_response")
        valid = (
            not body.get("error")
            and task.get("status", {}).get("state") in COMPLETED
            and len(calls) == 1
            and len(responses) == 1
            and calls[0]["id"] == responses[0]["id"]
            and calls[0]["name"] == expected_name
            and calls[0].get("args") == expected_args
        )
        if not valid or not (require_final or terminal_marker):
            return valid
        if terminal_marker:
            return json.loads(answer(task)) == {"done": True}
        from .workflow_evidence import tool_value
        expected = tool_value(responses[0]["response"])
        # The stock Go ADK records a FastMCP dict return as {"output": ...}
        # after it has already removed structuredContent/content transport
        # fields. The model-facing business JSON is the inner object.
        expected = business_json(expected)
        return business_json(json.loads(answer(task))) == expected
    except (ValueError, KeyError, TypeError):
        return False


def wait_adapter_ready(endpoint, timeout_seconds=ADAPTER_READY_TIMEOUT_SECONDS):
    """Wait for non-inference adapter health before creating a probe intent."""
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    with httpx.Client(timeout=5, transport=httpx.HTTPTransport(retries=0),
                      follow_redirects=False) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(endpoint.rstrip("/") + "/healthz")
                if response.status_code == 200:
                    return
                last_error = RuntimeError("adapter health status " + str(response.status_code))
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(2)
    raise RuntimeError("adapter endpoint not reachable before create-only intent") from last_error


def readonly_serving_probe(store, workflow):
    """One exact Recommendation call, separate from offline and A/B suites."""
    member = members(workflow)["recommendation"]
    return _run_probe(
        store,
        workflow,
        "recommendation-serving-v2",
        "recommendation",
        member,
        '{"user_id":218,"candidate_item_ids":null,"top_k":3}',
        "get_personalized_recommendations",
        {"user_id": 218, "candidate_item_ids": None, "top_k": 3},
        None,
    )


def _run_probe(store, workflow, probe_id, role, member, prompt, expected_name,
               expected_args, stock_runtime_image, coordinator_children=None,
               timeout_seconds=REQUEST_TIMEOUT_SECONDS):
    root = "workflow/runtime-preflights/" + workflow["release_id"] + "/" + probe_id
    try:
        result = json.loads(store.client.get_object(
            Bucket=store.bucket, Key=root + "/result.json")["Body"].read())
    except ClientError as error:
        if error.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
            raise
        adapter_rendered = (role == "coordinator" and
            workflow.get("runtime", {}).get("deterministic_output")
            in {"a2a-results-v1", "trusted-child-tool-results-v2"})
        endpoint = (("http://" + resource_name(member)
            + ".kagent.svc.cluster.local/") if adapter_rendered else
            ("http://kagent-controller.kagent.svc.cluster.local:8083/"
             "api/a2a-sandboxes/kagent/" + resource_name(member) + "/"))
        # DNS/EndpointSlice propagation can lag Deployment readiness. This GET
        # never invokes an agent and happens before the durable send intent, so
        # it cannot turn a health retry into an inference replay.
        if adapter_rendered:
            wait_adapter_ready(endpoint)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, root))
        request = {"jsonrpc": "2.0", "id": request_id, "method": "SendMessage",
                   "params": {"message": {"messageId": request_id,
                   "contextId": request_id, "role": "ROLE_USER",
                   "parts": [{"kind": "text", "text": prompt}],
                   "metadata": {"source": "infrastructure_test"}}}}
        intent = {"source": "infrastructure_test", "request": request,
                  "inference_budget": 1, "llama_cpp_build": "b8646",
                  "wall_clock_timeout_seconds": timeout_seconds}
        if stock_runtime_image:
            intent["stock_runtime_image"] = stock_runtime_image
        try:
            prior_intent = json.loads(store.client.get_object(
                Bucket=store.bucket, Key=root + "/intent.json")["Body"].read())
        except ClientError as error:
            if error.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
                raise
            store.client.put_object(Bucket=store.bucket, Key=root + "/intent.json",
                Body=json.dumps(intent).encode(), ContentType="application/json", IfNoneMatch="*")
            prior_intent = None
        if prior_intent is not None:
            if prior_intent != intent:
                raise ValueError("stock probe intent identity mismatch; do not replay")
            # A Jenkins/helper crash after SendMessage is not permission to
            # infer again. Recover only the one task created by the deterministic
            # context ID and the authenticated principal.
            from apps.agentic.llm_ab_router.child_tasks import ChildTasks
            root_agent = "kagent__NS__" + resource_name(member).replace("-", "_")
            reader = ChildTasks("workflow-infrastructure-preflight", [root_agent])
            try:
                body = reader(request_id)
            except Exception as exc:
                raise ValueError("stock probe sent but task evidence is not recoverable; do not replay") from exc
            finally:
                reader.close()
            if (role == "coordinator" and workflow.get("runtime", {}).get(
                    "deterministic_output") in {
                        "a2a-results-v1", "trusted-child-tool-results-v2"}):
                from apps.agentic.llm_ab_router.server import render_workflow_result
                tools = {r: next("kagent__NS__" + t["agent"]["name"].replace("-", "_")
                    for t in member["agent"]["tools"] if "-" + r + "-" in t["agent"]["name"])
                    for r in ("context", "recommendation")}
                child_reader = None
                if workflow["runtime"]["deterministic_output"] == "trusted-child-tool-results-v2":
                    child_reader = ChildTasks(
                        "workflow-infrastructure-preflight", list(tools.values()))
                try:
                    body, _ = render_workflow_result(
                        body, tools["context"], tools["recommendation"],
                        child_reader=child_reader,
                        output_profile=workflow["runtime"]["deterministic_output"])
                finally:
                    if child_reader:
                        child_reader.close()
            result = _result_from_body(body, workflow, probe_id, role, expected_name,
                                       expected_args, request_id, coordinator_children)
            result["recovered_from_existing_task"] = True
        else:
            try:
                with httpx.Client(timeout=timeout_seconds,
                                  transport=httpx.HTTPTransport(retries=0),
                                  follow_redirects=False) as client, \
                        wall_clock_deadline(timeout_seconds):
                    request_headers = {"x-user-id": "workflow-infrastructure-preflight",
                                       "a2a-version": "1.0"}
                    if adapter_rendered:
                        request_headers["authorization"] = (
                            "Bearer " + os.environ["AB_INTERNAL_TOKEN"])
                    response = client.post(
                        endpoint,
                        json=request,
                        headers=request_headers)
                    response.raise_for_status()
                    body = response.json()
                result = _result_from_body(body, workflow, probe_id, role, expected_name,
                                           expected_args, request_id, coordinator_children)
            except (httpx.HTTPError, WallClockDeadline) as exc:
                result = {"source": "infrastructure_test", "role": role,
                    "release_id": workflow["release_id"], "probe": probe_id,
                    "verdict": "FAIL", "request_id": request_id,
                    "reason": "timeout_or_transport_failure_no_retry",
                    "error_type": type(exc).__name__, "llama_cpp_build": "b8646",
                    "offline_requests": 0, "synthetic_requests": 0}
        store.client.put_object(Bucket=store.bucket, Key=root + "/result.json",
            Body=json.dumps(result).encode(), ContentType="application/json",
            IfNoneMatch="*")
    if result["verdict"] != "PASS":
        raise ValueError("stock probe failed for " + role + "; no retry")
    summary = {k: result[k] for k in ("source", "role", "release_id", "probe",
        "verdict", "offline_requests", "synthetic_requests")}
    if role == "coordinator":
        summary["compatibility_gate"] = result["compatibility_gate"]
        summary["child_task_evidence_checksum"] = result["child_task_evidence_checksum"]
    return summary


def _result_from_body(body, workflow, probe_id, role, expected_name,
                      expected_args, request_id, coordinator_children=None):
    """Build a verdict from a terminal task, whether live or recovered."""
    child_task_owner_verified = None
    child_task_evidence_checksum = None
    coordinator_gate = None
    if role == "coordinator":
        # The stock remote-A2A responses are the trusted sources of child
        # session IDs. Read every child as the original principal and require
        # exact native outer calls, exact inner calls and exact final rendering.
        specs = coordinator_children or []
        root_task = task_of(body)
        calls = events(root_task, "function_call")
        responses = events(root_task, "function_response")
        expected_calls = list(zip(
            expected_name if isinstance(expected_name, list) else [expected_name],
            expected_args if isinstance(expected_args, list) else [expected_args],
        ))
        expected_names = [name for name, _args in expected_calls]
        expected_route_args = [args for _name, args in expected_calls]
        observed_names = [call.get("name") for call in calls]
        observed_route_args = [call.get("args") for call in calls]
        paired_response_ids = (
            len(calls) == len(responses)
            and all(call.get("id") == response.get("id")
                    for call, response in zip(calls, responses))
        )
        ask_user_calls = sum(call.get("name") == "ask_user" for call in calls)
        coordinator_gate = {
            "expected_a2a_calls": expected_names,
            "observed_function_calls": observed_names,
            "expected_route_arguments": expected_route_args,
            "observed_route_arguments": observed_route_args,
            "ask_user_calls": ask_user_calls,
            "extra_function_calls": max(0, len(calls) - len(expected_calls)),
            "terminal_state": root_task.get("status", {}).get("state"),
            "terminal_output_verified": False,
            "trajectory_verified": False,
        }
        child_task_owner_verified = bool(
            not body.get("error")
            and root_task.get("status", {}).get("state") in COMPLETED
            and len(calls) == len(responses) == len(expected_calls) == len(specs)
            and observed_names == expected_names
            and observed_route_args == expected_route_args
            and paired_response_ids
            and ask_user_calls == 0
        )
        coordinator_gate["trajectory_verified"] = child_task_owner_verified
        child_outputs, child_evidence = {}, []
        if child_task_owner_verified:
            from apps.agentic.llm_ab_router.child_tasks import ChildTasks
            try:
                for call, spec in zip(calls, specs):
                    response = next(r for r in responses if r["id"] == call["id"])
                    child_session = response.get("response", {}).get("subagent_session_id")
                    if not child_session:
                        raise ValueError("missing child session")
                    reader = ChildTasks("workflow-infrastructure-preflight", [spec["agent"]])
                    try:
                        child = reader(child_session)
                    finally:
                        reader.close()
                    marker = (workflow.get("runtime", {}).get(
                        "specialist_terminal_output") == "done-marker-v1")
                    if not stock_probe_verdict(child, spec["tool"], spec["args"],
                                               require_final=not marker,
                                               terminal_marker=marker):
                        raise ValueError("child tool or final output mismatch")
                    from .workflow_evidence import tool_value
                    child_response = events(task_of(child), "function_response")[0]
                    child_outputs[spec["role"]] = business_json(
                        tool_value(child_response["response"]))
                    child_evidence.append(child)
                expected_final = (next(iter(child_outputs.values()))
                                  if len(child_outputs) == 1 else child_outputs)
                child_task_owner_verified = json.loads(answer(root_task)) == expected_final
                coordinator_gate["terminal_output_verified"] = child_task_owner_verified
            except Exception:
                # Any missing/unauthorized child evidence is a terminal probe
                # failure. The create-only intent prevents using this failure
                # as permission to replay an A2A or MCP call.
                child_task_owner_verified = False
                coordinator_gate["terminal_output_verified"] = False
        if child_task_owner_verified:
            child_task_evidence_checksum = digest(child_evidence)
        probe_valid = child_task_owner_verified
    else:
        marker = (workflow.get("runtime", {}).get(
            "specialist_terminal_output") == "done-marker-v1")
        probe_valid = stock_probe_verdict(
            body, expected_name, expected_args,
            require_final=role == "recommendation" and not marker,
            terminal_marker=marker)
    verdict = "PASS" if probe_valid else "FAIL"
    result = {"source": "infrastructure_test", "role": role,
        "release_id": workflow["release_id"], "probe": probe_id,
        "verdict": verdict, "request_id": request_id, "evidence": body,
        "llama_cpp_build": "b8646", "offline_requests": 0,
        "synthetic_requests": 0}
    if role == "coordinator":
        result.update(child_task_owner_verified=child_task_owner_verified,
                      child_task_evidence_checksum=child_task_evidence_checksum,
                      compatibility_gate=coordinator_gate)
        if not probe_valid:
            result["reason"] = "exact_native_coordinator_trajectory_gate_failed"
    return result


def readonly_stock_probes(store, workflow, stock_runtime_image):
    """Six create-only probes covering both A2A routes and their composition."""
    variants = members(workflow)
    recommendation_request = (
        "Call get_personalized_recommendations exactly once with "
        "user_id=218, candidate_item_ids=null, top_k=3")
    context_request = (
        "Call get_user_online_features exactly once with arguments "
        '{"user_id":218,"candidate_item_ids":null,"top_k":2}. '
        "Its schema accepts JSON null. Copy null exactly; never use []. "
        "After its response make zero more function calls."
    )
    recommendation_agent = "kagent__NS__" + resource_name(
        variants["recommendation"]).replace("-", "_")
    context_agent = "kagent__NS__" + resource_name(
        variants["context"]).replace("-", "_")
    context_a2a_request = (
        "Call get_chunk_by_id exactly once with "
        "chunk_id='800005:product_overview:overview:0'")
    recommendation_child = {"role": "recommendation", "agent": recommendation_agent,
        "tool": "get_personalized_recommendations",
        "args": {"user_id": 218, "candidate_item_ids": None, "top_k": 3}}
    context_child = {"role": "context", "agent": context_agent,
        "tool": "get_chunk_by_id",
        "args": {"chunk_id": "800005:product_overview:overview:0"}}
    cases = [
        ("stock-recommendation-v29", "recommendation", variants["recommendation"],
         recommendation_request + ". Return the complete response object, not only items.",
         "get_personalized_recommendations",
         {"user_id": 218, "candidate_item_ids": None, "top_k": 3}, None),
        ("stock-context-exact-null-v29", "context", variants["context"],
         context_request, "get_user_online_features",
         {"user_id": 218, "candidate_item_ids": None, "top_k": 2}, None),
        ("stock-context-exact-chunk-v29", "context", variants["context"],
         'Call get_chunk_by_id exactly once with chunk_id="800005:product_overview:overview:0"',
         "get_chunk_by_id", {"chunk_id": "800005:product_overview:overview:0"}, None),
        ("stock-coordinator-recommendation-a2a-owner-v29", "coordinator", variants["coordinator"],
         "MODE=SINGLE_RECOMMENDATION\nRECOMMENDATION_REQUEST=" + recommendation_request,
         recommendation_agent, {"request": "RECOMMENDATION_REQUEST=" + recommendation_request},
         [recommendation_child]),
        ("stock-coordinator-context-a2a-owner-v29", "coordinator", variants["coordinator"],
         "MODE=SINGLE_CONTEXT\nCONTEXT_REQUEST=" + context_a2a_request,
         context_agent, {"request": "CONTEXT_REQUEST=" + context_a2a_request},
         [context_child]),
        ("stock-coordinator-composite-a2a-owner-v29", "coordinator", variants["coordinator"],
         "MODE=COMPOSITE\nRECOMMENDATION_REQUEST=" + recommendation_request
         + "\nCONTEXT_REQUEST=" + context_a2a_request,
         [recommendation_agent, context_agent],
         [{"request": recommendation_request}, {"request": context_a2a_request}],
         [recommendation_child, context_child]),
    ]
    reports, failures = [], []
    for probe_id, role, member, prompt, name, args, children in cases:
        try:
            reports.append(_run_probe(store, workflow, probe_id, role, member,
                prompt, name, args, stock_runtime_image, children))
        except ValueError:
            failures.append(probe_id)
    if failures:
        raise ValueError("stock compatibility failed for " + ",".join(failures)
                         + "; fixed matrix completed once without replay")
    return reports


def _readonly_coordinator_a2a_probes(store, workflow, stock_runtime_image, prefix):
    """Three create-only full-A2A probes with a caller-owned evidence prefix.

    These are infrastructure tests, not offline compatibility or the twenty
    online A/B cases. They deliberately exercise the Coordinator and its real
    child A2A agents before any Istio allocation can reach the candidate.
    """
    variants = members(workflow)
    recommendation_request = (
        "Call get_personalized_recommendations exactly once with "
        "user_id=218, candidate_item_ids=null, top_k=3")
    context_request = (
        "Call get_chunk_by_id exactly once with "
        "chunk_id='800005:product_overview:overview:0'")
    recommendation_agent = "kagent__NS__" + resource_name(
        variants["recommendation"]).replace("-", "_")
    context_agent = "kagent__NS__" + resource_name(
        variants["context"]).replace("-", "_")
    recommendation_child = {"role": "recommendation", "agent": recommendation_agent,
        "tool": "get_personalized_recommendations",
        "args": {"user_id": 218, "candidate_item_ids": None, "top_k": 3}}
    context_child = {"role": "context", "agent": context_agent,
        "tool": "get_chunk_by_id",
        "args": {"chunk_id": "800005:product_overview:overview:0"}}
    cases = [
        (prefix + "-recommendation-a2a-v33",
         "MODE=SINGLE_RECOMMENDATION\nRECOMMENDATION_REQUEST=" + recommendation_request,
         recommendation_agent, {"request": recommendation_request},
         [recommendation_child]),
        (prefix + "-context-a2a-v33",
         "MODE=SINGLE_CONTEXT\nCONTEXT_REQUEST=" + context_request,
         context_agent, {"request": context_request},
         [context_child]),
        (prefix + "-composite-a2a-v33",
         "MODE=COMPOSITE\nRECOMMENDATION_REQUEST=" + recommendation_request
         + "\nCONTEXT_REQUEST=" + context_request,
         [recommendation_agent, context_agent],
         [{"request": recommendation_request}, {"request": context_request}],
         [recommendation_child, context_child]),
    ]
    reports, failures = [], []
    for probe_id, prompt, name, args, children in cases:
        try:
            reports.append(_run_probe(
                store, workflow, probe_id, "coordinator", variants["coordinator"],
                prompt, name, args, stock_runtime_image, children,
                timeout_seconds=CANDIDATE_A2A_TIMEOUT_SECONDS))
        except ValueError:
            failures.append(probe_id)
    if failures:
        raise ValueError("Coordinator full-A2A preflight failed for "
                         + ",".join(failures)
                         + "; no canary and no replay")
    return reports


def readonly_prompt_baseline_probes(store, workflow, stock_runtime_image):
    """Validate a prompt-only baseline before its route can be activated."""
    return _readonly_coordinator_a2a_probes(
        store, workflow, stock_runtime_image, "prompt-baseline-coordinator")


def readonly_candidate_a2a_probes(store, workflow, stock_runtime_image):
    """Validate the frozen-prompt LLM candidate before creating an experiment."""
    return _readonly_coordinator_a2a_probes(
        store, workflow, stock_runtime_image, "candidate-coordinator")


def candidate_coordinator_probe_ids():
    """Return the immutable full-A2A gate expected by workflow CD."""
    return [
        "candidate-coordinator-recommendation-a2a-v33",
        "candidate-coordinator-context-a2a-v33",
        "candidate-coordinator-composite-a2a-v33",
    ]


def verified_candidate_a2a_evidence(store, workflow):
    """Read and validate the create-only Coordinator gate before canary state.

    The three direct offline model checks deliberately cannot satisfy this
    gate: this evidence must come from the native Coordinator calling the real
    A2A specialists and reaching a terminal root result at zero traffic.
    """
    release_id = workflow["release_id"]
    reports = []
    for probe_id in candidate_coordinator_probe_ids():
        key = (
            "workflow/runtime-preflights/"
            + release_id
            + "/"
            + probe_id
            + "/result.json"
        )
        try:
            response = store.client.get_object(Bucket=store.bucket, Key=key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {
                "NoSuchKey",
                "NoSuchObject",
                "404",
            }:
                raise ValueError(
                    "HOLD: native Coordinator candidate preflight evidence is missing"
                ) from error
            raise
        report = json.loads(response["Body"].read())
        gate = report.get("compatibility_gate", {})
        if (
            report.get("source") != "infrastructure_test"
            or report.get("role") != "coordinator"
            or report.get("release_id") != release_id
            or report.get("probe") != probe_id
            or report.get("verdict") != "PASS"
            or report.get("offline_requests") != 0
            or report.get("synthetic_requests") != 0
            or report.get("child_task_owner_verified") is not True
            or not re.fullmatch(
                r"[0-9a-f]{64}", report.get("child_task_evidence_checksum", "")
            )
            or gate.get("trajectory_verified") is not True
            or gate.get("terminal_output_verified") is not True
            or gate.get("terminal_state") != "TASK_STATE_COMPLETED"
            or gate.get("ask_user_calls") != 0
            or gate.get("extra_function_calls") != 0
            or gate.get("observed_function_calls")
            != gate.get("expected_a2a_calls")
            or gate.get("observed_route_arguments")
            != gate.get("expected_route_arguments")
        ):
            raise ValueError(
                "FAIL: native Coordinator candidate preflight did not pass: "
                + probe_id
            )
        reports.append(report)
    return {
        "verdict": "PASS",
        "release_id": release_id,
        "probe_ids": candidate_coordinator_probe_ids(),
        "evidence_checksum": digest(reports),
    }
