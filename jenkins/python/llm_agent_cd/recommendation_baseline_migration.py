"""Audited Recommendation prompt/output baseline migration under Jenkins lock."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import re
import time

from botocore.exceptions import ClientError

from .driver import Driver
from .release import release
from .release_guard import check
from .state import StateStore


NULL_AND_OUTPUT_CONTRACT = """

Native Recommendation tool-call invariants:
- JSON null is a valid candidate_item_ids value. When the current request says
  candidate_item_ids=null, emit JSON null exactly. Never replace null with [],
  an invented list, a range, or omitted arguments.
- After the single tool response, finish immediately. The trusted A/B adapter
  renders that already-executed tool response as structured JSON without any
  additional model or tool call.
"""

LEGACY_MISSING_USER_TERMINAL_CONTRACT = """

Missing-user terminal invariant:
- If the current user message does not explicitly provide user_id, call no
  function at all. In particular, never call ask_user and never call
  get_personalized_recommendations.
- Finish the task immediately with exactly this JSON object and no markdown or
  extra text: {"status":"clarification_required","missing":["user_id"]}
"""

LEGACY_NATIVE_MISSING_USER_CONTRACT = """

Missing-user native clarification invariant:
- If the current user message does not explicitly provide user_id, never call
  get_personalized_recommendations or any MCP/dependency tool.
- Use the native ask_user tool exactly once to request only the missing user_id,
  then wait for the user's answer. Do not guess a value and do not repeat the
  question. TASK_STATE_INPUT_REQUIRED is the expected safe terminal state for
  this turn.
"""

MISSING_USER_TERMINAL_CONTRACT = """

Missing-user deterministic clarification invariant:
- Determine user_id only from the current user message. A transport identity,
  an agent-card example, model memory, or a common value such as 1 or 1001 is
  never a recommendation user_id.
- If the current user message does not explicitly contain user_id as an
  integer, call no function at all. Never call ask_user and never call
  get_personalized_recommendations.
- A sentence saying that user_id was not provided is still missing user_id;
  do not infer or invent one.
- Finish immediately with exactly this JSON object and no markdown or extra
  text: {"status":"clarification_required","missing":["user_id"]}
"""

CANONICAL_RECOMMENDATION_PROMPT = """You are the RecSys Recommendation Agent. Apply the first matching rule only.

1. TERMINAL TOOL RESULT: If the latest message is the response from your one
   get_personalized_recommendations call, make no further function call. Do
   not reconsider the earlier user request. Finish immediately; the trusted
   A/B adapter renders that tool response as the final structured JSON.
2. MISSING USER: If the last message is a user request without an explicit
   integer user_id, never use a transport identity, memory, examples, or a
   common value such as 1 or 1001. Never call an MCP/dependency tool. Call
   ask_user exactly once with exactly:
   {"questions":[{"question":"Please provide your user_id as an integer."}]}
   Then stop in INPUT_REQUIRED. Never retry ask_user after its pending result.
   An integer is an explicit user_id only when the user associates it with
   `user_id` or `user ID`, for example `user_id=1001` or `user ID 1001`.
   A requested item count is never a user_id. In particular, for the request
   `Recommend three items. I have not provided a user ID.`, call ask_user as
   above and never call get_personalized_recommendations.
3. NEW USER REQUEST WITH ID: Only if the latest message is a user request with
   an explicit integer user_id, call get_personalized_recommendations exactly
   once. Copy user_id, candidate_item_ids, and top_k. If candidate_item_ids is
   omitted, use JSON null. Preserve an explicit null; never invent, reuse,
   reorder, or replace arguments.

Never call another agent or another MCP tool. Never retry or duplicate a call.
Preserve the tool result byte-for-byte: item order, item_id, score,
model_version, A/B metadata, and item metadata. Empty items is valid.
"""


def migrated(champion: dict, adapter_image: str) -> dict:
    champion = release(champion)
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", adapter_image):
        raise ValueError("baseline adapter image must be digest pinned")
    value = {
        "config": deepcopy(champion["config"]),
        "llm": deepcopy(champion["llm"]),
        "agent": deepcopy(champion["agent"]),
        "binding": deepcopy(champion["binding"]),
    }
    # Freeze a compact canonical prompt rather than accumulating migration
    # fragments. This avoids contradictory legacy instructions and keeps the
    # thinking-model control inside its fixed serving reasoning budget.
    value["agent"]["systemMessage"] = CANONICAL_RECOMMENDATION_PROMPT
    value["binding"].update(
        adapter_image=adapter_image,
        adapter_replicas=1,
        grpc_transport=True,
        recommendation_output_profile="trusted-tool-result-v1",
        release_schema_version=2,
    )
    return release(value)


def _publish(store: StateStore, value: dict) -> str:
    key = "recommendation/releases/" + value["release_id"] + ".json"
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    try:
        store.client.put_object(
            Bucket=store.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            IfNoneMatch="*",
        )
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
            raise
        if store.client.get_object(Bucket=store.bucket, Key=key)["Body"].read() != body:
            raise ValueError("immutable baseline manifest collision") from exc
    return "s3://" + store.bucket + "/" + key


def reconcile_terminal_route(store: StateStore, state: dict, etag: str, driver: Driver):
    """Repair only a zero-weight terminal route whose release pointers agree.

    An older rollback persisted ``disabled`` after rendering the route.  That
    left an unreachable pin route in Envoy even though allocation remained
    100% on the champion.  Baseline migration runs under the shared Jenkins
    release lock, so it can safely reconcile this exact metadata-only drift.
    No agent request is issued by route or verify_route.
    """
    if driver.verify_route(state, 0, state["route_revision"]):
        return state, etag
    pointers = {
        state.get(key, {}).get("release_id")
        for key in ("champion", "baseline", "pending")
    }
    if (
        state.get("phase") not in {"IDLE", "COMPLETED", "ROLLED_BACK"}
        or state.get("verified_weight") != 0
        or len(pointers) != 1
        or None in pointers
    ):
        raise ValueError("current Recommendation champion route is not verified")
    repaired = deepcopy(state)
    revision = driver.route(repaired, 0)
    deadline = time.monotonic() + 60
    while not driver.verify_route(repaired, 0, revision) and time.monotonic() < deadline:
        time.sleep(2)
    if not driver.verify_route(repaired, 0, revision):
        raise ValueError("terminal Recommendation route reconciliation not acknowledged")
    repaired["route_revision"] = revision
    repaired["route_intent"] = {"weight": 0, "next_phase": repaired["phase"]}
    repaired.setdefault("events", []).append(
        {
            "at": time.time(),
            "phase": repaired["phase"],
            "action": "terminal_route_reconciled",
            "route_revision": revision,
        }
    )
    return repaired, store.write(repaired, etag)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("Recommendation baseline migration requires Jenkins")
    os.environ.update(json.loads(open(os.environ["AB_ENV_FILE"]).read()))
    os.environ.update(
        AB_SCOPE="recommendation",
        AB_ROUTER_IMAGE=args.image,
        AB_SECRET_NAME="recsys-llm-ab-runtime",
    )
    check("recommendation")
    store = StateStore(os.environ["AB_STATE_URI"])
    state, etag = store.read()
    if state.get("phase") not in {"IDLE", "COMPLETED", "ROLLED_BACK"}:
        raise ValueError("unfinished Recommendation operation")
    old = release(state["champion"])
    new = migrated(old, args.image)
    if new["release_id"] == old["release_id"]:
        raise ValueError("Recommendation baseline migration is a NOOP")
    driver = Driver()
    state, etag = reconcile_terminal_route(store, state, etag, driver)
    reference = store.archive(state) if state.get("experiment_id") else None
    history = list(state.get("history", []))
    if reference and reference not in history:
        history.append(reference)
    uri = _publish(store, new)
    state.update(
        phase="BASELINE_MIGRATING",
        migration={
            "kind": "recommendation-prompt-output-v9",
            "from": old["release_id"],
            "to": new["release_id"],
            "manifest": uri,
            "build_url": os.environ["BUILD_URL"],
        },
        baseline=old,
        pending=new,
        history=history,
        releases={**state.get("releases", {}), new["release_id"]: new},
        route_intent={"weight": 0, "next_phase": "BASELINE_MIGRATING"},
    )
    etag = store.write(state, etag)
    try:
        driver.deploy(old)
        driver.deploy(new)
        driver.verify_release(old)
        driver.verify_release(new)
        final = deepcopy(state)
        final.update(baseline=new, pending=new)
        revision = driver.route(final, 0)
        if not driver.verify_route(final, 0, revision):
            raise ValueError("new Recommendation baseline route not acknowledged")
        final.update(
            phase="IDLE",
            champion=new,
            previous=old,
            route_revision=revision,
            verified_weight=0,
            route_intent={"weight": 0, "next_phase": "IDLE"},
            gate={"verdict": "PASS", "reason": "baseline prompt/output migration"},
        )
        final.pop("experiment_id", None)
        store.write(final, etag)
        print(json.dumps({
            "phase": "IDLE",
            "champion_release_id": new["release_id"],
            "previous_release_id": old["release_id"],
            "prompt_checksum": __import__("hashlib").sha256(
                new["agent"]["systemMessage"].encode()
            ).hexdigest(),
            "route_revision": revision,
        }, sort_keys=True))
    except Exception:
        rollback = deepcopy(state)
        # Quarantine the failed release before rendering the compensation
        # route.  Persisting this only after routing used to leave a stale pin
        # route that disagreed with durable state.
        rollback.update(
            baseline=old,
            pending=new,
            disabled=sorted(set(rollback.get("disabled", [])) | {new["release_id"]}),
        )
        revision = driver.route(rollback, 0)
        if not driver.verify_route(rollback, 0, revision):
            rollback.update(phase="ROLLBACK_FAILED")
            store.write(rollback, etag)
        else:
            rollback.update(
                phase="ROLLED_BACK",
                champion=old,
                pending=old,
                route_revision=revision,
                verified_weight=0,
                gate={"verdict": "FAIL", "reason": "baseline migration failed"},
            )
            store.write(rollback, etag)
        raise


if __name__ == "__main__":
    main()
