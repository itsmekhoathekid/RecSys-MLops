"""Six candidate-only native tool-call checks used during model onboarding."""

from __future__ import annotations

import json
import os
import re
import time

import httpx
from psycopg.types.json import Jsonb

from apps.agentic.llm_ab_router.database import Database
from .release import digest
from .small_compatibility import RECOMMENDATION_TOOLS, generation_parameters


CASES = (
    "tool_selection",
    "arguments",
    "single_tool_call",
    "structured_tool_result",
    "empty_result",
    "missing_user",
)

ONBOARDING_CONTRACT = """You are validating a Recommendation model.
If the latest message is a user request with an explicit integer user_id, call
get_personalized_recommendations exactly once and copy user_id,
candidate_item_ids, and top_k exactly. If user_id is missing, do not call any
function; return exactly {"questions":[{"question":"Please provide your user_id as an integer."}]}.
If the latest message is a tool result, do not call another function. Return
the tool result as one JSON object with no prose, markdown, or changed values.
"""


def fixtures(contract: str):
    system = {"role": "system", "content": contract}
    ranked = {"items": [
        {"item_id": 7, "score": 0.8, "metadata": {"source": "onboarding"}},
        {"item_id": 4, "score": 0.5, "metadata": {}},
    ]}

    def tool_result(value):
        return [
            system,
            {"role": "user", "content": "Recommend 2 items for user_id=1001."},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "onboarding-call", "type": "function", "function": {
                    "name": "get_personalized_recommendations",
                    "arguments": '{"user_id":1001,"candidate_item_ids":null,"top_k":2}',
                },
            }]},
            {"role": "tool", "tool_call_id": "onboarding-call", "content": json.dumps(value)},
        ]

    return [
        ("tool_selection", [system, {"role": "user", "content":
            "Use the recommendation tool for user_id=1001, candidate_item_ids=null, top_k=3."}],
         ("tool", {"user_id": 1001, "candidate_item_ids": None, "top_k": 3})),
        ("arguments", [system, {"role": "user", "content":
            "Recommend for user_id=1002 using candidate_item_ids=[101,102] and top_k=2."}],
         ("tool", {"user_id": 1002, "candidate_item_ids": [101, 102], "top_k": 2})),
        ("single_tool_call", [system, {"role": "user", "content":
            "Call the recommendation tool exactly once for user_id=1003, candidate_item_ids=null, top_k=1."}],
         ("tool", {"user_id": 1003, "candidate_item_ids": None, "top_k": 1})),
        ("structured_tool_result", tool_result(ranked), ("json", ranked)),
        ("empty_result", tool_result({"items": []}), ("json", {"items": []})),
        ("missing_user", [system, {"role": "user", "content":
            "Recommend two items, but I have not provided a user ID. Return a JSON clarification and do not call a tool."}],
         ("missing", None)),
    ]


def _json_content(message):
    text = (message.get("content") or "").strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    return json.loads(text)


def check(message, assertion):
    calls = message.get("tool_calls") or []
    kind, expected = assertion
    if kind == "tool":
        if len(calls) != 1 or calls[0].get("function", {}).get("name") != "get_personalized_recommendations":
            return False
        return json.loads(calls[0]["function"]["arguments"]) == expected
    if calls:
        return False
    try:
        value = _json_content(message)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if kind == "json":
        return value == expected
    return isinstance(value, dict) and bool(re.search(r"user.?id", json.dumps(value), re.I))


def response_format(assertion):
    kind = assertion[0]
    if kind == "json":
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "item_id": {"type": "integer"},
                            "score": {"type": "number"},
                            "metadata": {"type": "object"},
                        },
                        "required": ["item_id", "score", "metadata"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        }
    elif kind == "missing":
        schema = {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {"question": {"type": "string"}},
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["questions"],
            "additionalProperties": False,
        }
    else:
        return None
    return {
        "type": "json_schema",
        "json_schema": {"name": "recommendation_compatibility", "strict": True, "schema": schema},
    }


def run(db: Database, manifest: dict, onboarding_id: str, call):
    expected = fixtures(ONBOARDING_CONTRACT)
    if tuple(case for case, _, _ in expected) != CASES:
        raise ValueError("onboarding compatibility suite drift")
    results = []
    for case_id, messages, assertion in expected:
        with db.connect() as connection:
            claimed = connection.execute(
                """INSERT INTO recsys_ab.onboarding_compatibility(onboarding_id,case_id,status)
                   VALUES(%s,%s,'CLAIMED') ON CONFLICT DO NOTHING RETURNING case_id""",
                (onboarding_id, case_id),
            ).fetchone()
        if not claimed:
            continue
        started = time.monotonic()
        result = {"verdict": "FAIL", "response_checksum": None}
        try:
            body = {
                "model": manifest["binding"]["model_alias"],
                "messages": messages,
                "tools": RECOMMENDATION_TOOLS,
                **generation_parameters(manifest),
            }
            output_schema = response_format(assertion)
            if output_schema:
                body["response_format"] = output_schema
            response = call(body)
            message = response["choices"][0]["message"]
            result = {
                "verdict": "PASS" if check(message, assertion) else "FAIL",
                "response_checksum": digest(response),
            }
        except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError) as exc:
            result["error_type"] = type(exc).__name__
        latency = time.monotonic() - started
        with db.connect() as connection:
            connection.execute(
                """UPDATE recsys_ab.onboarding_compatibility
                   SET status=%s,result=%s,latency_seconds=%s,finished_at=now()
                   WHERE onboarding_id=%s AND case_id=%s AND status='CLAIMED'""",
                (result["verdict"], Jsonb(result), latency, onboarding_id, case_id),
            )
        results.append({"case_id": case_id, **result, "latency_seconds": latency})
    return results


def job(intent: dict, image: str, namespace: str = "kagent") -> dict:
    onboarding_id = intent["onboarding_id"]
    if not re.fullmatch(r"onb-[0-9a-f]{32}", onboarding_id):
        raise ValueError("invalid onboarding ID")
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image):
        raise ValueError("compatibility image must be digest pinned")
    candidate = intent["candidate"]
    env = [
        {"name": "AB_ONBOARDING_ID", "value": onboarding_id},
        {"name": "AB_ONBOARDING_CANDIDATE", "value": json.dumps(candidate, sort_keys=True)},
        {"name": "CANDIDATE_API_KEY", "valueFrom": {"secretKeyRef": {
            "name": candidate["binding"]["api_key_secret"],
            "key": candidate["binding"]["api_key_secret_key"],
        }}},
    ]
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": "llm-onboard-" + onboarding_id[4:], "namespace": namespace,
                     "labels": {"app": "recsys-llm-onboarding", "recsys.ai/owner": "llm-agent-cd"}},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 900, "ttlSecondsAfterFinished": 86400,
                 "template": {"metadata": {"labels": {"app": "recsys-llm-onboarding"},
                                           "annotations": {"sidecar.istio.io/inject": "false"}},
                              "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                                       "nodeSelector": {"recsys.ai/pool": "ml-system"},
                                       "tolerations": [{"key": "recsys.ai/workload", "operator": "Equal",
                                                        "value": "ml-system", "effect": "NoSchedule"}],
                                       "containers": [{"name": "compatibility", "image": image,
                                                       "command": ["python", "-m", __name__],
                                                       "envFrom": [{"secretRef": {"name": "recsys-llm-ab-runtime"}}],
                                                       "env": env,
                                                       "resources": {"requests": {"cpu": "50m", "memory": "128Mi"},
                                                                     "limits": {"cpu": "500m", "memory": "256Mi"}}}]}}},
    }


def main():
    db = Database(os.environ["AB_DATABASE_URL"])
    db.migrate()
    manifest = json.loads(os.environ["AB_ONBOARDING_CANDIDATE"])

    def call(body):
        with httpx.Client(timeout=120, follow_redirects=False,
                          transport=httpx.HTTPTransport(retries=0)) as client:
            response = client.post(
                manifest["binding"]["backend_url"].rstrip("/") + "/chat/completions",
                json=body,
                headers={"Authorization": "Bearer " + os.environ["CANDIDATE_API_KEY"]},
            )
            response.raise_for_status()
            return response.json()

    result = run(db, manifest, os.environ["AB_ONBOARDING_ID"], call)
    if any(row["verdict"] != "PASS" for row in result):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
