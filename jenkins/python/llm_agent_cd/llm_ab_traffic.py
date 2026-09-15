"""Create and run the single durable Recommendation A/B Traffic Job.

The local ``start`` command only creates or attaches to a Kubernetes Job.  The
in-cluster ``run`` command emits public, signed HTTPS traffic and never calls
Jenkins or mutates Istio.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import time

import httpx

from apps.agentic.llm_ab_router.database import Database
from apps.agentic.llm_ab_router.tickets import claims_for_live, sign
from jenkins.python.llm_agent_cd.external_cases import DEFAULT_URL, _body, run as run_cases
from jenkins.python.llm_agent_cd.release import digest
from jenkins.python.llm_agent_cd.state import StateStore

EXPERIMENT = re.compile(r"rec-[0-9a-f]{32}")
TERMINAL = {"COMPLETED", "ROLLED_BACK", "ROLLBACK_FAILED"}


def _kubectl(namespace: str, *args: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["kubectl", "-n", namespace, *args], input=stdin, text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "kubectl failed")
    return result.stdout


def job_name(experiment_id: str) -> str:
    if not EXPERIMENT.fullmatch(experiment_id):
        raise ValueError("invalid Recommendation experiment ID")
    return "ab-traffic-" + experiment_id[4:]


def job_manifest(experiment_id: str, image: str, namespace: str, *,
                 node_selector=None, tolerations=None) -> dict:
    if not re.search(r"@sha256:[0-9a-f]{64}$", image):
        raise ValueError("Traffic Job image must be digest pinned")
    name = job_name(experiment_id)
    identity = digest({
        "schema_version": 1, "experiment_id": experiment_id, "image": image,
        "node_selector": node_selector or {}, "tolerations": tolerations or [],
        "entrypoint": "public-a2a",
    })
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app": "recsys-ab-traffic",
                "recsys.ai/experiment-id": experiment_id,
            },
            "annotations": {"recsys.ai/traffic-manifest-checksum": identity},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 4 * 60 * 60,
            "ttlSecondsAfterFinished": 24 * 60 * 60,
            "template": {
                "metadata": {
                    "labels": {
                        "app": "recsys-ab-traffic",
                        "recsys.ai/experiment-id": experiment_id,
                    },
                    "annotations": {"sidecar.istio.io/inject": "false"},
                },
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": "recsys-ab-traffic",
                    "automountServiceAccountToken": False,
                    "nodeSelector": node_selector or {},
                    "tolerations": tolerations or [],
                    "containers": [{
                        "name": "traffic",
                        "image": image,
                        "command": [
                            "python", "-m",
                            "jenkins.python.llm_agent_cd.llm_ab_traffic", "run",
                            "--experiment-id", experiment_id,
                            "--entrypoint", "public-a2a",
                        ],
                        "envFrom": [
                            {"secretRef": {"name": "recsys-llm-ab-runtime"}},
                            {"secretRef": {"name": "recsys-recommendation-ab-traffic-auth"}},
                        ],
                        "env": [{"name": "AB_STATE_URI", "value":
                                 "s3://recsys-llm-ab/recommendation/state.json"}],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "128Mi"},
                            "limits": {"cpu": "500m", "memory": "256Mi"},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }],
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 1000,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                },
            },
        },
    }


def start(experiment_id: str, *, namespace: str, follow: bool) -> dict:
    name = job_name(experiment_id)
    claim_name = "ab-traffic-claim-" + experiment_id[4:]
    existing = _kubectl(namespace, "get", "job", name, "--ignore-not-found", "-o", "json")
    claim = _kubectl(namespace, "get", "configmap", claim_name, "--ignore-not-found", "-o", "json")
    created = not bool(claim.strip())
    if created:
        if existing.strip():
            raise ValueError("Traffic Job exists without its durable claim")
        deployment = json.loads(_kubectl(
            namespace, "get", "deployment", "recsys-ab-router", "-o", "json"
        ))
        image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        pod_spec = deployment["spec"]["template"]["spec"]
        manifest = job_manifest(
            experiment_id, image, namespace,
            node_selector=pod_spec.get("nodeSelector", {}),
            tolerations=pod_spec.get("tolerations", []),
        )
        identity = manifest["metadata"]["annotations"]["recsys.ai/traffic-manifest-checksum"]
        marker = {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": claim_name, "namespace": namespace,
                         "labels": {"app": "recsys-ab-traffic-claim"}},
            "immutable": True,
            "data": {"experiment_id": experiment_id, "job_name": name,
                     "manifest_checksum": identity},
        }
        # The marker is intentionally created first. A crash afterward causes
        # HOLD/rollback rather than authorizing a replacement Traffic Job.
        _kubectl(namespace, "create", "-f", "-", stdin=json.dumps(marker))
        _kubectl(namespace, "create", "-f", "-", stdin=json.dumps(manifest))
    else:
        marker = json.loads(claim)
        if marker.get("data", {}).get("experiment_id") != experiment_id:
            raise ValueError("Traffic Job claim identity conflict")
        if not existing.strip():
            raise RuntimeError("Traffic Job was lost after durable claim; no replacement created")
        current = json.loads(existing)
        if current.get("metadata", {}).get("annotations", {}).get(
            "recsys.ai/traffic-manifest-checksum"
        ) != marker.get("data", {}).get("manifest_checksum"):
            raise ValueError("Traffic Job immutable manifest conflict")
    result = {"status": "CREATED" if created else "ATTACHED", "job_name": name,
              "experiment_id": experiment_id}
    print(json.dumps(result, sort_keys=True), flush=True)
    if follow:
        subprocess.run(["kubectl", "-n", namespace, "logs", "-f", "job/" + name])
    return result


def _send_live(state: dict, db: Database, client: httpx.Client) -> None:
    run = db.traffic_run(state["experiment_id"])
    if not run or run["live_submitted"] >= 360:
        time.sleep(10)
        return
    sequence = run["live_submitted"] + 1
    fixtures = state.get("fixtures", [])
    if len(fixtures) != 20 or digest(fixtures) != state.get("fixture_checksum"):
        raise ValueError("live traffic requires the frozen 20-case fixture suite")
    fixture = fixtures[(sequence - 1) % len(fixtures)]
    prompt = fixture.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("live traffic fixture prompt is invalid")
    request_id = digest([state["experiment_id"], "live_test", sequence])
    claims = claims_for_live(
        state["experiment_id"], state["phase"], request_id, prompt,
        secrets.token_hex(24),
    )
    if db.issue_live_ticket(claims) is None:
        time.sleep(10)
        return
    started = time.monotonic()
    try:
        response = client.post(
            os.environ.get("AB_EXTERNAL_A2A_URL", DEFAULT_URL),
            json=_body(request_id, prompt),
            headers={"X-RecSys-AB-Ticket": sign(claims, os.environ["AB_CASE_TICKET_KEY"]),
                     "A2A-Version": "1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        revision = response.headers.get("x-recsys-ab-edge-revision", "")
        if payload.get("id") != request_id or not revision:
            raise httpx.ProtocolError("edge response identity is ambiguous")
        # The edge commits COMPLETED before returning the response. It is the
        # authoritative terminal writer; the traffic client must not write the
        # same ticket a second time.
        status = "COMPLETED"
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        status = db.mark_live_ambiguous(request_id, type(exc).__name__) or "AMBIGUOUS"
    print(json.dumps({"event": "ab.traffic", "experiment_id": state["experiment_id"],
                      "phase": state["phase"], "kind": "live_test",
                      "sequence": sequence, "status": status}), flush=True)
    time.sleep(max(0.0, 10.0 - (time.monotonic() - started)))


def _ready_to_drain(state: dict, now: float | None = None) -> bool:
    """Stop issuing live traffic once the controller has a mature PASS gate.

    The controller owns the decision and writes the gate snapshot.  The traffic
    runner only observes that snapshot and becomes quiescent so the controller
    can verify there are no in-flight requests before changing Istio weights.
    """
    if state.get("gate", {}).get("verdict") != "PASS":
        return False
    policy = state.get("policy", {})
    window = policy.get("window_seconds")
    started = state.get("stage_started")
    if not isinstance(window, (int, float)) or not isinstance(started, (int, float)):
        return False
    return (time.time() if now is None else now) - started >= window


def run(experiment_id: str) -> int:
    db = Database(os.environ["AB_DATABASE_URL"])
    db.migrate()
    store = StateStore(os.environ["AB_STATE_URI"])
    name = job_name(experiment_id)
    identity = digest({"schema_version": 1, "experiment_id": experiment_id,
                       "job_name": name, "entrypoint": "public-a2a"})
    _, created = db.begin_traffic_run(experiment_id, name, identity)
    if not created:
        print(json.dumps({"event": "ab.traffic.refused", "experiment_id": experiment_id,
                          "reason": "durable traffic run already exists"}), flush=True)
        return 2
    key = os.environ.get("AB_CASE_TICKET_KEY", "")
    username = os.environ.get("AB_EDGE_BASIC_USER", "")
    password = os.environ.get("AB_EDGE_BASIC_PASSWORD", "")
    if len(key.encode()) < 32 or not username or not password:
        db.finish_traffic_run(experiment_id, "FAILED", "edge credentials unavailable")
        raise ValueError("edge credentials unavailable")
    client = httpx.Client(
        auth=httpx.BasicAuth(username, password), verify=True,
        follow_redirects=False, transport=httpx.HTTPTransport(retries=0),
        timeout=httpx.Timeout(600),
    )
    try:
        while True:
            state, _ = store.read()
            if state.get("experiment_id") != experiment_id:
                trigger_status = db.trigger_status(experiment_id)
                if trigger_status in {
                    "COMPLETED", "REJECTED", "ROLLED_BACK", "ROLLBACK_FAILED"
                }:
                    db.finish_traffic_run(
                        experiment_id, "COMPLETED",
                        "experiment terminated before traffic: " + trigger_status,
                    )
                    return 0
                time.sleep(10)
                continue
            phase = state.get("phase")
            db.heartbeat_traffic_run(experiment_id, phase)
            if phase in TERMINAL:
                db.finish_traffic_run(experiment_id, "COMPLETED")
                return 0
            if phase in {"CANARY", "VERIFY"}:
                if _ready_to_drain(state):
                    time.sleep(2)
                else:
                    _send_live(state, db, client)
            elif phase == "AB":
                if db.source_inflight(experiment_id, "live_test"):
                    time.sleep(2)
                    continue
                suite = db.external_suite(experiment_id)
                if not suite["run"]:
                    run_cases(
                        state,
                        database=db,
                        client=client,
                        progress=lambda: db.heartbeat_traffic_run(
                            experiment_id, "AB"
                        ),
                    )
                elif _ready_to_drain(state):
                    time.sleep(2)
                else:
                    _send_live(state, db, client)
            else:
                time.sleep(10)
    except Exception as exc:
        db.finish_traffic_run(experiment_id, "FAILED", type(exc).__name__)
        raise
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Durable Recommendation A/B traffic")
    sub = parser.add_subparsers(dest="command", required=True)
    start_parser = sub.add_parser("start")
    run_parser = sub.add_parser("run")
    for item in (start_parser, run_parser):
        item.add_argument("--experiment-id", required=True)
        item.add_argument("--entrypoint", choices=["public-a2a"], default="public-a2a")
    start_parser.add_argument("--namespace", default="kagent")
    start_parser.add_argument("--follow", action="store_true")
    args = parser.parse_args()
    if args.command == "start":
        start(args.experiment_id, namespace=args.namespace, follow=args.follow)
        return 0
    return run(args.experiment_id)


if __name__ == "__main__":
    raise SystemExit(main())
