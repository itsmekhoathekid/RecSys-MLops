#!/usr/bin/env python3
"""Idempotently configure public gateway A records through Porkbun.

Credentials are read only from the process environment and are never printed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


API_BASE = "https://api.porkbun.com/api/json/v3"
DOMAIN = os.getenv("GATEWAY_DNS_DOMAIN", "recsys-mlops.site")
NAMES = tuple(
    name.strip()
    for name in os.getenv("GATEWAY_DNS_NAMES", "agents,registry,rag").split(",")
    if name.strip()
)
ADDRESS = os.getenv("GATEWAY_DNS_ADDRESS", "136.85.106.59")
TTL = os.getenv("GATEWAY_DNS_TTL", "600")


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


AUTH = {
    "apikey": required_env("PORKBUN_API_KEY"),
    "secretapikey": required_env("PORKBUN_SECRET_API_KEY"),
}


def request(path: str, payload: dict[str, str]) -> dict:
    data = json.dumps({**AUTH, **payload}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/{path}",
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "recsys-gateway-dns/1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"Porkbun API returned HTTP {exc.code}: {detail[:500]}") from exc
    if result.get("status") != "SUCCESS":
        raise RuntimeError(f"Porkbun API request failed: {result.get('message', 'unknown error')}")
    return result


def upsert(name: str) -> str:
    encoded_name = urllib.parse.quote(name, safe="")
    result = request(f"dns/retrieveByNameType/{DOMAIN}/A/{encoded_name}", {})
    records = result.get("records", [])

    if any(record.get("content") == ADDRESS for record in records):
        return "unchanged"

    payload = {"type": "A", "name": name, "content": ADDRESS, "ttl": TTL}
    if records:
        record_id = records[0].get("id")
        if not record_id:
            raise RuntimeError(f"existing Porkbun record for {name}.{DOMAIN} is missing its id")
        request(f"dns/edit/{DOMAIN}/{record_id}", payload)
        return "updated"

    request(f"dns/create/{DOMAIN}", payload)
    return "created"


def main() -> int:
    if not NAMES:
        raise RuntimeError("GATEWAY_DNS_NAMES must contain at least one record name")
    for name in NAMES:
        action = upsert(name)
        print(f"DNS {action}: {name}.{DOMAIN} -> {ADDRESS}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
