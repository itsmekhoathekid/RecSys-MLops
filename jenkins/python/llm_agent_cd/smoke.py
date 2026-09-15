"""One explicitly requested infrastructure smoke, never part of the 20-case suite.

SendMessage is sent once. Polling is read-only. Ambiguous failures are not retried.
The output file is an operational artifact, not fabricated experiment evidence.
"""

import argparse
import json
import time
import uuid
from pathlib import Path

import requests

from .evidence import task_of
from .provision import forward


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent")
    parser.add_argument("--service", default="recsys-ab-router")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--prompt",
        default="Recommend top 3 items for user_id=1001. Use candidate_item_ids=null. Preserve the recommendation tool result and return it as JSON.",
    )
    args = parser.parse_args()
    request_id = str(uuid.uuid4())
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": request_id,
                "contextId": request_id,
                "role": "ROLE_USER",
                "parts": [{"kind": "text", "text": args.prompt}],
            }
        },
    }
    service = "kagent-controller" if args.agent else args.service
    remote_port = 8083 if args.agent else 80
    with forward("kagent", service, remote_port) as base:
        url = (
            base + "/api/a2a-sandboxes/kagent/" + args.agent + "/"
            if args.agent
            else base + "/"
        )
        session = requests.Session()
        session.headers.update(
            {"A2A-Version": "1.0", "X-User-ID": "ab-infrastructure-smoke"}
        )
        card = session.get(url + ".well-known/agent-card.json", timeout=20)
        card.raise_for_status()
        start = time.monotonic()
        response = session.post(url, json=body, timeout=600)
        response.raise_for_status()
        payload = response.json()
        while task_of(payload).get("status", {}).get("state") in {
            "submitted",
            "working",
            "TASK_STATE_SUBMITTED",
            "TASK_STATE_WORKING",
        }:
            if time.monotonic() - start > 600:
                raise RuntimeError("smoke incomplete; do not replay")
            time.sleep(1)
            response = session.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "GetTask",
                    "params": {"id": task_of(payload)["id"], "contextId": request_id},
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
    task = task_of(payload)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "request_id": request_id,
                "agent": args.agent or args.service,
                "infrastructure_only": True,
                "elapsed_seconds": time.monotonic() - start,
                "response": payload,
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "request_id": request_id,
                "state": task.get("status"),
                "error": payload.get("error"),
                "artifact": str(path),
            }
        )
    )
    if payload.get("error") or task.get("status", {}).get("state") not in {
        "completed",
        "TASK_STATE_COMPLETED",
    }:
        raise RuntimeError("smoke did not complete; do not replay")


if __name__ == "__main__":
    main()
