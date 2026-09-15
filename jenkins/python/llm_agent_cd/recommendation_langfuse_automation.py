"""Provision and operate the Recommendation-only Langfuse rollout trigger."""

from __future__ import annotations

import argparse
import json

import requests

from apps.agentic.llm_ab_router.trigger import (
    PARKING_LABEL,
    PARKING_PROMPT_TEXT,
    PROMPT_TEXT,
    READY_LABEL,
    validate_config,
)
from .langfuse_automation import Admin
from .provision import apply, forward, secret

PROJECT = "recsys-production"
PROMPT = "recsys-recommendation-ab"
NAME = "RecSys recommendation A/B dispatch"
URL = "https://agents.recsys-mlops.site/webhooks/langfuse-recommendation"
SECRET = "recsys-recommendation-webhook-auth"
TRIGGER_SECRET = "recsys-recommendation-trigger"
PARKING_CONFIG = {"schema_version": 0, "scope": "recommendation", "kind": "ab-label-parking"}


def public_session(values):
    session = requests.Session()
    session.trust_env = False
    session.auth = (values["project-public-key"], values["project-secret-key"])
    return session


def exact_prompt(session, url, version):
    response = session.get(url + "/api/public/v2/prompts/" + PROMPT,
                           params={"version": version}, timeout=20)
    response.raise_for_status()
    return response.json()


def move_labels(session, url, version, labels):
    response = session.patch(url + "/api/public/v2/prompts/" + PROMPT + "/versions/" + str(version),
                             json={"newLabels": labels}, timeout=20)
    response.raise_for_status()


def ensure_parking(move_ready=False):
    """Create/verify the non-runnable version used to remove pointer labels."""
    langfuse = secret("langfuse", "recsys-langfuse-runtime")
    with forward("langfuse", "langfuse-web", 3000) as url:
        session = public_session(langfuse)
        response = session.get(url + "/api/public/v2/prompts", params={
            "name": PROMPT, "label": PARKING_LABEL, "limit": 10}, timeout=20)
        response.raise_for_status()
        rows = response.json().get("data", [])
        versions = [v for row in rows if row.get("name") == PROMPT
                    for v in row.get("versions", [])]
        if len(set(versions)) > 1:
            raise ValueError("multiple Langfuse parking versions")
        if versions:
            value = exact_prompt(session, url, versions[0])
            created = False
        else:
            response = session.post(url + "/api/public/v2/prompts", json={
                "name": PROMPT, "type": "text", "prompt": PARKING_PROMPT_TEXT,
                "labels": [PARKING_LABEL], "config": PARKING_CONFIG,
                "commitMessage": "Create non-runnable A/B label parking version",
            }, timeout=30)
            response.raise_for_status()
            value = response.json()
            created = True
        if (value.get("name") != PROMPT or value.get("prompt") != PARKING_PROMPT_TEXT
                or value.get("config") != PARKING_CONFIG
                or PARKING_LABEL not in value.get("labels", [])):
            raise ValueError("Langfuse parking version round-trip mismatch")
        if move_ready:
            move_labels(session, url, value["version"], [READY_LABEL])
            value = exact_prompt(session, url, value["version"])
            if READY_LABEL not in value.get("labels", []):
                raise ValueError("ab-ready was not confirmed on parking version")
        return {"name": PROMPT, "version": value["version"], "created": created,
                "ready_parked": READY_LABEL in value.get("labels", [])}


def status():
    """Return non-secret automation and prompt-label state for cutover checks."""
    langfuse = secret("langfuse", "recsys-langfuse-runtime")
    with forward("langfuse", "langfuse-web", 3000) as url:
        admin = Admin(url, langfuse["initial-admin-password"])
        automations = [row for row in admin.call(
            "getAutomations", {"projectId": PROJECT, "eventSource": "prompt"}
        ) if row["name"] == NAME]
        session = public_session(langfuse)
        response = session.get(url + "/api/public/v2/prompts", params={
            "name": PROMPT, "limit": 100}, timeout=20)
        response.raise_for_status()
        versions = sorted({version for row in response.json().get("data", [])
                           if row.get("name") == PROMPT for version in row.get("versions", [])})
        prompts = [exact_prompt(session, url, version) for version in versions]
        return {
            "automation": [{"id": row["id"], "status": row["trigger"]["status"]}
                           for row in automations],
            "versions": [{"version": row["version"], "labels": sorted(row.get("labels", [])),
                          "parking": row.get("prompt") == PARKING_PROMPT_TEXT}
                         for row in prompts],
        }


def automation_config(status="INACTIVE"):
    return {
        "projectId": PROJECT,
        "name": NAME,
        "eventSource": "prompt",
        "eventAction": ["created", "updated"],
        "filter": [{"column": "Name", "type": "string", "operator": "=", "value": PROMPT}],
        "status": status,
        "actionType": "WEBHOOK",
        "actionConfig": {
            "type": "WEBHOOK",
            "url": URL,
            "apiVersion": {"prompt": "v1"},
            "requestHeaders": {},
        },
    }


def prepare():
    from .provision import kube

    old = kube("kagent", "get", "secret", SECRET, "--ignore-not-found", "-o", "json")
    if old and json.loads(old)["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd":
        raise ValueError("foreign Recommendation webhook secret")
    langfuse = secret("langfuse", "recsys-langfuse-runtime")
    with forward("langfuse", "langfuse-web", 3000) as url:
        client = Admin(url, langfuse["initial-admin-password"])
        rows = client.call("getAutomations", {"projectId": PROJECT, "eventSource": "prompt"})
        owned = [row for row in rows if row["name"] == NAME]
        if owned:
            if len(owned) != 1 or not old:
                raise ValueError("Recommendation automation requires secret reconciliation")
            saved = secret("kagent", SECRET)
            automation = owned[0]
            if (automation["id"] != saved["automation_id"]
                    or automation["action"]["config"]["url"] != URL
                    or automation["trigger"]["filter"] != automation_config()["filter"]):
                raise ValueError("Recommendation automation drift")
            return {"automation_id": automation["id"], "status": automation["trigger"]["status"],
                    "prompt": PROMPT, "created": False}
        if old:
            raise ValueError("webhook secret exists but Recommendation automation is missing")
        result = client.call("createAutomation", automation_config(), write=True)
        generated = result.get("webhookSecret")
        if not isinstance(generated, str) or not generated:
            raise ValueError("Recommendation webhook HMAC secret missing")
        automation_id = result["automation"]["id"]
        apply("kagent", {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": SECRET,
            "labels": {"recsys.ai/owner": "llm-agent-cd"}}, "stringData": {
                "LANGFUSE_WEBHOOK_SECRET": generated, "automation_id": automation_id,
                "project_id": PROJECT, "prompt_name": PROMPT, "endpoint": URL}})
        return {"automation_id": automation_id, "status": "INACTIVE", "prompt": PROMPT, "created": True}


def set_status(status):
    if status not in {"ACTIVE", "INACTIVE"}:
        raise ValueError("invalid automation status")
    saved = secret("kagent", SECRET)
    if status == "ACTIVE":
        dispatch = secret("kagent", TRIGGER_SECRET)
        if not dispatch or dispatch.get("AB_DISPATCH_ENABLED") != "true":
            raise ValueError("Recommendation poller dispatch is not enabled")
    langfuse = secret("langfuse", "recsys-langfuse-runtime")
    with forward("langfuse", "langfuse-web", 3000) as url:
        client = Admin(url, langfuse["initial-admin-password"])
        rows = client.call("getAutomations", {"projectId": PROJECT, "eventSource": "prompt"})
        owned = [row for row in rows if row["name"] == NAME]
        if len(owned) != 1:
            raise ValueError("Recommendation automation identity ambiguous")
        automation = owned[0]
        if (automation["id"] != saved["automation_id"]
                or automation["action"]["config"]["url"] != URL
                or automation["trigger"]["filter"] != automation_config()["filter"]):
            raise ValueError("Recommendation automation drift")
        if automation["trigger"]["status"] != status:
            client.call("updateAutomation", {
                **automation_config(status), "automationId": automation["id"]
            }, write=True)
        rows = client.call("getAutomations", {"projectId": PROJECT, "eventSource": "prompt"})
        if next(row for row in rows if row["id"] == automation["id"])["trigger"]["status"] != status:
            raise ValueError("Recommendation automation status not confirmed")
        return {"automation_id": automation["id"], "status": status, "prompt": PROMPT}


def publish(config):
    config = validate_config(config, allow_live_test=True)
    if config["scope"] != "recommendation":
        raise ValueError("Recommendation prompt requires recommendation scope")
    langfuse = secret("langfuse", "recsys-langfuse-runtime")
    payload = {"name": PROMPT, "type": "text", "prompt": PROMPT_TEXT,
               "labels": ["ab-ready"], "config": config,
               "commitMessage": "Dispatch immutable Recommendation A/B candidate"}
    with forward("langfuse", "langfuse-web", 3000) as url:
        session = public_session(langfuse)
        current = session.get(url + "/api/public/v2/prompts/" + PROMPT, timeout=20)
        if current.status_code == 200:
            value = current.json()
            if (value.get("prompt") == PROMPT_TEXT and value.get("config") == config
                    and "ab-ready" in value.get("labels", [])):
                return {"name": PROMPT, "version": value["version"], "created": False}
        response = session.post(url + "/api/public/v2/prompts", json=payload, timeout=30)
        response.raise_for_status()
        value = response.json()
        if (value.get("name") != PROMPT or value.get("prompt") != PROMPT_TEXT
                or value.get("config") != config or "ab-ready" not in value.get("labels", [])):
            raise ValueError("Langfuse Recommendation prompt round-trip mismatch")
        return {"name": PROMPT, "version": value["version"], "created": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "activate", "deactivate", "publish",
                                           "parking", "park-ready", "status"])
    parser.add_argument("--config")
    args = parser.parse_args()
    if args.action == "prepare":
        result = prepare()
    elif args.action == "activate":
        result = set_status("ACTIVE")
    elif args.action == "deactivate":
        result = set_status("INACTIVE")
    elif args.action == "parking":
        result = ensure_parking(False)
    elif args.action == "park-ready":
        result = ensure_parking(True)
    elif args.action == "status":
        result = status()
    else:
        if not args.config:
            parser.error("publish requires --config")
        result = publish(json.loads(open(args.config).read()))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
