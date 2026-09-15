"""Signed, body-bound, one-shot identities for the public Recommendation A/B edge."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from jenkins.python.llm_agent_cd.release import digest

VERSION = "v2"
LEGACY_VERSION = "v1"
TTL_SECONDS = 65 * 60
LEGACY_REQUIRED = {
    "experiment_id",
    "case_id",
    "request_id",
    "fixture_checksum",
    "prompt_sha256",
    "expires_at",
    "nonce",
}
REQUIRED = LEGACY_REQUIRED | {"traffic_kind", "phase"}


def _wire(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise ValueError("malformed ticket encoding") from exc


def prompt_from_body(body: dict) -> str:
    """Accept the one supported A2A SendMessage shape and nothing else."""
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        raise ValueError("invalid JSON-RPC envelope")
    if body.get("method") not in {"SendMessage", "message/send"}:
        raise ValueError("ticket only authorizes SendMessage")
    message = body.get("params", {}).get("message")
    if not isinstance(message, dict):
        raise ValueError("A2A message is required")
    request_id = message.get("messageId")
    if (
        not isinstance(request_id, str)
        or body.get("id") != request_id
        or message.get("contextId") != request_id
        or message.get("role") not in {"user", "ROLE_USER"}
    ):
        raise ValueError("ticket requires fresh, matching A2A identities")
    parts = message.get("parts")
    if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
        raise ValueError("ticket requires exactly one text part")
    if set(parts[0]) - {"kind", "text"} or parts[0].get("kind", "text") != "text":
        raise ValueError("unsupported A2A part")
    prompt = parts[0].get("text")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt is required")
    return prompt


def claims_for_case(
    experiment_id: str,
    case: dict,
    fixture_checksum: str,
    nonce: str,
    *,
    now: int | None = None,
    ttl_seconds: int = TTL_SECONDS,
) -> dict:
    issued = int(time.time() if now is None else now)
    if not all(
        isinstance(v, str) and v for v in (experiment_id, case.get("id"), nonce)
    ):
        raise ValueError("ticket identity fields are required")
    if ttl_seconds != TTL_SECONDS:
        raise ValueError("ticket TTL is fixed at 65 minutes")
    return {
        "experiment_id": experiment_id,
        "case_id": case["id"],
        "request_id": digest([experiment_id, case["id"]]),
        "fixture_checksum": fixture_checksum,
        "prompt_sha256": hashlib.sha256(case["prompt"].encode("utf-8")).hexdigest(),
        "expires_at": issued + ttl_seconds,
        "nonce": nonce,
        "traffic_kind": "synthetic_case",
        "phase": "AB",
    }


def claims_for_live(
    experiment_id: str,
    phase: str,
    request_id: str,
    prompt: str,
    nonce: str,
    *,
    now: int | None = None,
    ttl_seconds: int = TTL_SECONDS,
) -> dict:
    issued = int(time.time() if now is None else now)
    if phase not in {"CANARY", "AB", "VERIFY"}:
        raise ValueError("live traffic phase is invalid")
    if not all(isinstance(value, str) and value for value in (
        experiment_id, request_id, prompt, nonce
    )):
        raise ValueError("live ticket identity fields are required")
    if ttl_seconds != TTL_SECONDS:
        raise ValueError("ticket TTL is fixed at 65 minutes")
    return {
        "experiment_id": experiment_id,
        "case_id": "live-" + request_id[:24],
        "request_id": request_id,
        "fixture_checksum": "0" * 64,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "expires_at": issued + ttl_seconds,
        "nonce": nonce,
        "traffic_kind": "live_test",
        "phase": phase,
    }


def sign(claims: dict, secret: str) -> str:
    version = VERSION if "traffic_kind" in claims else LEGACY_VERSION
    _validate_claims(claims, version=version)
    if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
        raise ValueError("ticket key must contain at least 32 bytes")
    payload = _encode(_wire(claims))
    signed = (version + "." + payload).encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
    return version + "." + payload + "." + _encode(signature)


def verify(ticket: str, secret: str, *, now: int | None = None) -> dict:
    if not isinstance(ticket, str) or len(ticket) > 4096:
        raise ValueError("missing or oversized ticket")
    try:
        version, payload, supplied = ticket.split(".")
    except ValueError as exc:
        raise ValueError("malformed ticket") from exc
    if version not in {VERSION, LEGACY_VERSION}:
        raise ValueError("unsupported ticket version")
    signed = (version + "." + payload).encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
    if not hmac.compare_digest(_decode(supplied), expected):
        raise ValueError("invalid ticket signature")
    try:
        claims = json.loads(_decode(payload))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid ticket payload") from exc
    _validate_claims(claims, version=version)
    current = int(time.time() if now is None else now)
    if claims["expires_at"] < current:
        raise ValueError("expired ticket")
    return claims


def bind_body(claims: dict, body: dict) -> str:
    prompt = prompt_from_body(body)
    message = body["params"]["message"]
    if message["messageId"] != claims["request_id"]:
        raise ValueError("request ID does not match ticket")
    if not hmac.compare_digest(
        hashlib.sha256(prompt.encode("utf-8")).hexdigest(), claims["prompt_sha256"]
    ):
        raise ValueError("request body does not match ticket")
    return prompt


def _validate_claims(claims: dict, *, version: str | None = None) -> None:
    expected = LEGACY_REQUIRED if version == LEGACY_VERSION else REQUIRED
    if not isinstance(claims, dict) or set(claims) != expected:
        raise ValueError("ticket claims are not canonical")
    for key in expected - {"expires_at"}:
        if not isinstance(claims[key], str) or not claims[key]:
            raise ValueError("invalid ticket claim " + key)
    if not isinstance(claims["expires_at"], int) or isinstance(
        claims["expires_at"], bool
    ):
        raise ValueError("invalid ticket expiry")
    for key in ("request_id", "fixture_checksum", "prompt_sha256"):
        if len(claims[key]) != 64 or any(
            ch not in "0123456789abcdef" for ch in claims[key]
        ):
            raise ValueError("invalid digest claim " + key)
    if "traffic_kind" in claims:
        if claims["traffic_kind"] not in {"live_test", "synthetic_case"}:
            raise ValueError("invalid traffic kind")
        expected_phase = "AB" if claims["traffic_kind"] == "synthetic_case" else None
        if claims["phase"] not in {"CANARY", "AB", "VERIFY"}:
            raise ValueError("invalid traffic phase")
        if expected_phase and claims["phase"] != expected_phase:
            raise ValueError("synthetic cases are restricted to AB")
