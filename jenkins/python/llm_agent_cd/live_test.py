"""Bounded load on the trusted root router; never changes routes or tops up cases."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import time
import uuid
import httpx
from .state import StateStore
import re


def load_allowed(state, db):
    """Serialize the exact 20-case suite ahead of operational load at 50%."""
    return state["phase"] != "AB" or db.synthetic_suite_complete(
        state["experiment_id"], len(state["fixtures"])
    )


def job(experiment_id, image, secret="recsys-workflow-runtime", router_url=None):
    if not re.fullmatch(r"(?:wf|rec)-[0-9a-f]{32}", experiment_id) or not re.search(r"@sha256:[0-9a-f]{64}$", image):
        raise ValueError("load job requires a valid experiment ID and pinned image")
    router_url = router_url or (
        "http://recsys-ab-router.kagent.svc.cluster.local"
        if experiment_id.startswith("rec-")
        else "http://recsys-workflow-router.kagent.svc.cluster.local"
    )
    if not re.fullmatch(
        r"http://recsys-(?:ab|workflow)-router\.[a-z0-9-]+\.svc\.cluster\.local(?::[0-9]+)?",
        router_url,
    ):
        raise ValueError("live load router URL must be an internal trusted router")
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "ab-load-" + experiment_id,"labels":{"app":"recsys-agent-live-test"}},
            "spec": {"activeDeadlineSeconds": 3660, "backoffLimit": 0, "ttlSecondsAfterFinished": 86400,
                     "template": {"metadata": {"labels": {"app": "recsys-agent-live-test"},
                                                "annotations": {"sidecar.istio.io/inject": "false"}},
                                  "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                                           "nodeSelector": {"recsys.ai/pool": "ml-system"},
                                           "tolerations": [{"key":"recsys.ai/workload","operator":"Equal","value":"ml-system","effect":"NoSchedule"}],
                                           "securityContext": {"runAsNonRoot": True, "runAsUser": 1000},
                                           "containers": [{"name": "load", "image": image,
                                               "command": ["python", "-m", "jenkins.python.llm_agent_cd.live_test"],
                                               "envFrom": [{"secretRef": {"name": secret}}],
                                               "env": [
                                                   {"name": "AB_EXPERIMENT_ID", "value": experiment_id},
                                                   {"name": "AB_ROUTER_URL", "value": router_url},
                                               ],
                                               "resources": {"requests": {"cpu": "50m", "memory": "128Mi"},
                                                             "limits": {"cpu": "500m", "memory": "256Mi"}}}]}}}}


def main():
    required = (
        "AB_STATE_URI",
        "AB_DATABASE_URL",
        "AB_ROUTER_URL",
        "AB_INTERNAL_TOKEN",
        "AB_LIVE_TEST_TOKEN",
        "AB_EXPERIMENT_ID",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("live load missing required runtime settings: " + ",".join(missing))
    store = StateStore(os.environ["AB_STATE_URI"])
    eid = os.environ["AB_EXPERIMENT_ID"]
    from apps.agentic.llm_ab_router.database import Database
    db = Database(os.environ['AB_DATABASE_URL'])
    state, _ = store.read()
    if state.get('experiment_id') != eid or state['phase'] not in {'CANARY','AB','VERIFY'}:
        raise ValueError('live load requires its active experiment')
    if not db.claim_live_load(eid):
        print(json.dumps({'event':'agent.live_test','experiment_id':eid,
                          'outcome':'HOLD_existing_durable_claim_no_replay'}),flush=True)
        return
    sent = 0
    deadline = time.monotonic() + 3600
    def send(fixture):
        rid = str(uuid.uuid4())
        body = {"jsonrpc": "2.0", "id": rid, "method": "SendMessage", "params": {"message": {
            "messageId": rid, "contextId": rid, "role": "ROLE_USER", "parts": [{"kind": "text", "text": fixture["prompt"]}]}}}
        try:
            # No transport or application retries, even if the result is ambiguous.
            with httpx.Client(timeout=600, transport=httpx.HTTPTransport(retries=0)) as client:
                r = client.post(os.environ["AB_ROUTER_URL"].rstrip("/") + "/", json=body, headers={
                    "authorization": "Bearer " + os.environ["AB_INTERNAL_TOKEN"],
                    "x-recsys-source": "live_test", "x-recsys-test-token": os.environ["AB_LIVE_TEST_TOKEN"],
                    "x-user-id": "recommendation-live-test", "a2a-version": "1.0"})
                print(json.dumps({"event": "agent.live_test", "experiment_id": eid, "http_status": r.status_code}), flush=True)
        except httpx.HTTPError:
            print(json.dumps({"event": "agent.live_test", "experiment_id": eid, "outcome": "ambiguous_no_retry"}), flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = []
        while sent < 360 and time.monotonic() < deadline:
            state, _ = store.read()
            if state.get("experiment_id") != eid or state["phase"] in {"COMPLETED", "ROLLED_BACK", "ROLLBACK_FAILED"}:
                break
            done = [future for future in active if future.done()]
            for future in done:
                # Surface programming/configuration failures. HTTP ambiguity is
                # handled inside send() and is still never retried.
                future.result()
            active = [future for future in active if not future.done()]
            if not load_allowed(state, db):
                # Existing futures are allowed to drain. Do not claim another
                # live request while the exact synthetic suite owns the pool.
                time.sleep(10)
                continue
            if state["phase"] in {"CANARY", "AB", "VERIFY"} and len(active) < 2:
                if db.claim_live_request(eid) is None:
                    break
                # Same deterministic fixture mix for every arm; allocation stays with Istio.
                active.append(pool.submit(send, state["fixtures"][sent % 20]))
                sent += 1
            time.sleep(10)
        for future in active:
            future.result()

if __name__ == "__main__":
    main()
