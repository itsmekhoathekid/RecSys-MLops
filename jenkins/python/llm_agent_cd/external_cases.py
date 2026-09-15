"""One-shot Jenkins runner for the public production Recommendation A2A edge."""

from __future__ import annotations

import json
import os
import secrets
import time

import httpx

from apps.agentic.llm_ab_router.database import Database
from apps.agentic.llm_ab_router.tickets import claims_for_case, sign
from .release import digest

DEFAULT_URL = "https://agents.recsys-mlops.site/a2a/recommendation-ab/v1"


def _body(request_id: str, prompt: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": request_id,
                "contextId": request_id,
                "role": "ROLE_USER",
                "parts": [{"kind": "text", "text": prompt}],
            }
        },
    }


def run(
    state: dict,
    *,
    database=None,
    client=None,
    clock=time.monotonic,
    sleeper=time.sleep,
    wall_clock=time.time,
    progress=None,
) -> dict:
    """Send every frozen case once. An existing suite is evidence, not retry permission."""
    if (
        state.get("phase") != "AB"
        or state.get("champion", {}).get("scope", "recommendation") != "recommendation"
    ):
        raise ValueError("public case runner requires Recommendation AB phase")
    fixtures = state.get("fixtures", [])
    if len(fixtures) != 20 or len({case["id"] for case in fixtures}) != 20:
        raise ValueError("public case runner requires exactly 20 unique fixtures")
    fixture_checksum = digest(fixtures)
    if fixture_checksum != state.get("fixture_checksum"):
        raise ValueError("fixture checksum changed")
    key = os.environ.get("AB_CASE_TICKET_KEY", "")
    username = os.environ.get("AB_EDGE_BASIC_USER", "")
    password = os.environ.get("AB_EDGE_BASIC_PASSWORD", "")
    if len(key.encode("utf-8")) < 32 or not username or not password:
        raise ValueError("public edge credentials are unavailable")
    url = os.environ.get("AB_EXTERNAL_A2A_URL", DEFAULT_URL)
    if not url.startswith("https://"):
        raise ValueError("public Recommendation A/B edge must use HTTPS")
    db = database or Database(os.environ["AB_DATABASE_URL"])
    existing = db.external_suite(state["experiment_id"])
    if existing["run"]:
        print(
            json.dumps(
                {
                    "event": "ab.external_suite.reconcile",
                    "experiment_id": state["experiment_id"],
                    "status": existing["run"].get("status", "UNKNOWN"),
                }
            ),
            flush=True,
        )
        return existing
    issued_at = int(wall_clock())
    claims = [
        claims_for_case(
            state["experiment_id"],
            case,
            fixture_checksum,
            secrets.token_hex(24),
            now=issued_at,
        )
        for case in fixtures
    ]
    created = db.begin_external_suite(state["experiment_id"], fixture_checksum, claims)
    if not created:
        evidence = db.external_suite(state["experiment_id"])
        print(
            json.dumps(
                {
                    "event": "ab.external_suite.reconcile",
                    "experiment_id": state["experiment_id"],
                    "status": (evidence["run"] or {}).get("status", "UNKNOWN"),
                }
            ),
            flush=True,
        )
        return evidence

    owned_client = client is None
    http = client or httpx.Client(
        auth=httpx.BasicAuth(username, password),
        timeout=httpx.Timeout(state["policy"]["request_timeout_seconds"]),
        verify=True,
        follow_redirects=False,
        transport=httpx.HTTPTransport(retries=0),
    )
    progress = progress or (lambda: None)
    previous_start = None
    try:
        for case, ticket_claims in zip(fixtures, claims):
            progress()
            if previous_start is not None:
                sleeper(max(0.0, 10.0 - (clock() - previous_start)))
            previous_start = clock()
            case_id = case["id"]
            db.mark_ticket_intent(state["experiment_id"], case_id)
            started = clock()
            try:
                response = http.post(
                    url,
                    json=_body(ticket_claims["request_id"], case["prompt"]),
                    headers={
                        "X-RecSys-AB-Ticket": sign(ticket_claims, key),
                        "A2A-Version": "1.0",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                revision = response.headers.get("x-recsys-ab-edge-revision", "")
                if payload.get("id") != ticket_claims["request_id"] or not revision:
                    raise httpx.ProtocolError("edge response identity is ambiguous")
                print(
                    json.dumps(
                        {
                            "event": "ab.external_case",
                            "experiment_id": state["experiment_id"],
                            "case_id": case_id,
                            "status": "COMPLETED",
                            "latency_seconds": clock() - started,
                            "edge_revision": revision,
                        }
                    ),
                    flush=True,
                )
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                db.mark_external_ambiguous(
                    state["experiment_id"], case_id, type(exc).__name__
                )
                print(
                    json.dumps(
                        {
                            "event": "ab.external_case",
                            "experiment_id": state["experiment_id"],
                            "case_id": case_id,
                            "status": "AMBIGUOUS",
                            "latency_seconds": clock() - started,
                        }
                    ),
                    flush=True,
                )
            progress()
        return db.external_suite(state["experiment_id"])
    finally:
        if owned_client:
            http.close()
