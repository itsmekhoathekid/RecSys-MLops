"""Durable Langfuse rollout triggers for workflow and Recommendation.

Workflow keeps its authenticated webhook and finite Kubernetes dispatch Jobs.
Recommendation uses a one-minute poller and moves ``ab-ready`` through a
parking prompt before it submits Jenkins.  Neither trigger changes serving
routes; uncertain Jenkins submission is reconciled, never blindly retried.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote

import httpx
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from psycopg.types.json import Jsonb

from .database import DDL as ROUTER_DDL, Database, json_label
from jenkins.python.llm_agent_cd.release import (
    digest,
    managed_backend_binding,
    release,
    validate_experiment,
)
from jenkins.python.llm_agent_cd.state import StateStore
from jenkins.python.llm_agent_cd.workflow import ROLES, diff as workflow_diff
from jenkins.python.model_cd.storage import s3_client

PROMPT_TEXT = "Immutable RecSys agent rollout request. Edit config, then apply ab-ready."
LEGACY_PROMPT_TEXT = "Immutable RecSys workflow rollout request. Edit config, then apply ab-ready."
PARKING_PROMPT_TEXT = "Internal RecSys A/B label parking version. Never dispatch."
PARKING_LABEL = "ab-label-parking"
ACTIVE_LABEL = "ab-running"
READY_LABEL = "ab-ready"
TERMINAL_LABELS = {
    "COMPLETED": "ab-done",
    "REJECTED": "ab-fail",
    "ROLLED_BACK": "ab-fail",
    "ROLLBACK_FAILED": "ab-fail",
}

TARGETS = {
    "workflow": {
        "prefix": "wf",
        "state_env": "AB_WORKFLOW_STATE_URI",
        "state_default": "s3://recsys-llm-ab/workflow/state.json",
        "jenkins_env": "AB_WORKFLOW_JENKINS_JOB",
        "jenkins_default": "RecSys-LLM-Workflow-CD",
        "catalog_prefix": "workflow/catalog/",
        "candidate_prefix": "workflow/candidates/",
        "policy_refs": {
            "workflow-production": "configs/llm-ab/workflow-production-policy.json",
            "workflow-live-test": "configs/llm-ab/workflow-live-test-policy.json",
        },
    },
    "recommendation": {
        "prefix": "rec",
        "state_env": "AB_RECOMMENDATION_STATE_URI",
        "state_default": "s3://recsys-llm-ab/recommendation/state.json",
        "jenkins_env": "AB_RECOMMENDATION_JENKINS_JOB",
        "jenkins_default": "RecSys-LLM-Agent-CD",
        "catalog_prefix": "recommendation/catalog/",
        "candidate_prefix": "recommendation/candidates/",
        "policy_refs": {
            "recommendation-live-test": "configs/llm-ab/recommendation-live-test-policy.json",
        },
    },
}
DDL = """
CREATE SCHEMA IF NOT EXISTS recsys_ab;
CREATE TABLE IF NOT EXISTS recsys_ab.trigger_requests (
 experiment_id text PRIMARY KEY, prompt_name text NOT NULL, prompt_version integer NOT NULL,
 config jsonb NOT NULL, status text NOT NULL DEFAULT 'QUEUED', reason text,
 candidate_uri text, candidate jsonb, config_diff jsonb, job_name text,
 queue_id text, build_url text, submitted_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(prompt_name,prompt_version)
);
CREATE TABLE IF NOT EXISTS recsys_ab.trigger_events (
 event_id text PRIMARY KEY, payload_hash text NOT NULL,
 experiment_id text NOT NULL REFERENCES recsys_ab.trigger_requests,
 created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE recsys_ab.trigger_requests ADD COLUMN IF NOT EXISTS prompt_snapshot jsonb;
ALTER TABLE recsys_ab.trigger_events ADD COLUMN IF NOT EXISTS payload jsonb;
ALTER TABLE recsys_ab.trigger_requests ADD COLUMN IF NOT EXISTS project_id text;
ALTER TABLE recsys_ab.trigger_requests ADD COLUMN IF NOT EXISTS prompt_checksum text;
ALTER TABLE recsys_ab.trigger_requests ADD COLUMN IF NOT EXISTS label_state jsonb;
ALTER TABLE recsys_ab.trigger_requests ADD COLUMN IF NOT EXISTS label_sync_status text;
ALTER TABLE recsys_ab.trigger_requests ADD COLUMN IF NOT EXISTS label_sync_error text;
ALTER TABLE recsys_ab.trigger_requests ADD COLUMN IF NOT EXISTS labels_updated_at timestamptz;
CREATE UNIQUE INDEX IF NOT EXISTS trigger_prompt_snapshot_identity
  ON recsys_ab.trigger_requests(project_id,prompt_name,prompt_version,prompt_checksum)
  WHERE project_id IS NOT NULL AND prompt_checksum IS NOT NULL;
"""


def polling_mode():
    return (os.environ.get("AB_TRIGGER_MODE") == "poll"
            and os.environ.get("AB_TRIGGER_SCOPE") == "recommendation")


def canonical_prompt_snapshot(project_id, prompt):
    """Exclude mutable Langfuse labels from the immutable request identity."""
    return {
        "project_id": project_id,
        "name": prompt.get("name"),
        "version": prompt.get("version"),
        "type": prompt.get("type", "text"),
        "prompt": prompt.get("prompt"),
        "config": prompt.get("config"),
    }


def prompt_checksum(project_id, prompt):
    return digest(canonical_prompt_snapshot(project_id, prompt))


def signature_valid(raw, signature, secret):
    try:
        fields = dict(part.strip().split("=", 1) for part in signature.split(","))
        timestamp = int(fields["t"])
        expected = hmac.new(secret.encode(), fields["t"].encode() + b"." + raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, fields["v1"]), timestamp
    except (ValueError, KeyError):
        return False, 0


def target(scope):
    if scope not in TARGETS:
        raise ValueError("invalid rollout scope")
    return TARGETS[scope]


def scope_from_experiment(experiment_id):
    matches = [scope for scope, item in TARGETS.items()
               if re.fullmatch(item["prefix"] + r"-[0-9a-f]{32}", experiment_id)]
    if len(matches) != 1:
        raise ValueError("invalid experiment ID")
    return matches[0]


def state_uri(scope):
    item = target(scope)
    # AB_STATE_URI is retained as the workflow deployment's legacy setting.
    legacy = os.environ.get("AB_STATE_URI") if scope == "workflow" else None
    return os.environ.get(item["state_env"]) or legacy or item["state_default"]


def jenkins_job(scope):
    item = target(scope)
    legacy = os.environ.get("AB_JENKINS_JOB") if scope == "workflow" else None
    return os.environ.get(item["jenkins_env"]) or legacy or item["jenkins_default"]


def configured_experiment_pattern():
    """Limit a dedicated receiver/recovery loop to its authorized scope."""
    scope = os.environ.get("AB_TRIGGER_SCOPE")
    return target(scope)["prefix"] + "-%" if scope else "%"


def baseline_field(scope):
    return "baseline_workflow_release_id" if scope == "workflow" else "baseline_release_id"


def generation_field(scope):
    return "global_generation" if scope == "workflow" else "generation"


def validate_config(c, allow_live_test=None):
    if not isinstance(c, dict) or c.get("scope") not in TARGETS:
        raise ValueError("invalid rollout request schema")
    scope = c["scope"]
    required = {"schema_version", "scope", baseline_field(scope), generation_field(scope),
                "llm_release_ref", "experiment_type", "policy_ref"}
    allowed = required | ({"target_role"} if scope == "workflow" else set())
    if (not required <= set(c) or set(c) - allowed or c["schema_version"] != 1):
        raise ValueError("invalid " + scope + " request schema")
    if not re.fullmatch(r"[0-9a-f]{64}", c[baseline_field(scope)]):
        raise ValueError("invalid baseline ID")
    if not re.fullmatch(r"[0-9a-f]{64}", c["llm_release_ref"]):
        raise ValueError("LLM reference must be a content-addressed catalog ID")
    if c["experiment_type"] not in {"config_only", "llm_only", "combined"}:
        raise ValueError("invalid experiment type")
    if scope == "workflow" and c.get("target_role", "workflow") not in {"workflow", "coordinator"}:
        raise ValueError("invalid experiment target role")
    if scope == "workflow" and c.get("target_role") == "coordinator" and c["experiment_type"] != "llm_only":
        raise ValueError("coordinator target currently supports llm_only")
    if c["policy_ref"] not in target(scope)["policy_refs"]:
        raise ValueError("unknown policy")
    if (c["policy_ref"].endswith("live-test")
            and allow_live_test is not True
            and os.environ.get("AB_ALLOW_LIVE_TEST") != "true"):
        raise ValueError("live-test policy not enabled")
    if not isinstance(c[generation_field(scope)], dict):
        raise ValueError("generation must be an object")
    return c


def candidate_from_config(baseline, config, llm, namespace="kagent", allow_live_test=None):
    c = validate_config(config, allow_live_test=allow_live_test)
    baseline = release(baseline)
    scope = c["scope"]
    if (baseline.get("scope") == "workflow") != (scope == "workflow"):
        raise ValueError("baseline scope mismatch")
    if baseline["release_id"] != c[baseline_field(scope)]:
        raise ValueError("STALE_BASELINE: explicit new version required")
    if digest(llm) != c["llm_release_ref"]:
        raise ValueError("catalog identity mismatch")
    if scope == "recommendation":
        candidate = {key: deepcopy(baseline[key]) for key in
                     ("config", "llm", "agent", "binding")}
        candidate.update(config=deepcopy(c["generation"]), llm=deepcopy(llm))
        if digest(llm) != baseline["llm_version_id"]:
            llm_id = digest(llm)
            candidate["binding"] = managed_backend_binding(
                candidate["binding"], llm_id, namespace
            )
        candidate = release(candidate)
        validate_experiment(baseline, candidate, c["experiment_type"])
        return candidate
    candidate = {k: deepcopy(baseline[k]) for k in (
        "schema_version", "scope", "global_generation", "agent_overrides", "llm", "agents", "bindings")}
    for optional in ("llm_overrides", "change_scope"):
        if optional in baseline:
            candidate[optional] = deepcopy(baseline[optional])
    if "runtime" in baseline:
        candidate["runtime"] = deepcopy(baseline["runtime"])
    candidate.update(global_generation=deepcopy(c["global_generation"]), llm=deepcopy(llm))
    target_role = c.get("target_role", "workflow")
    if target_role == "coordinator":
        from jenkins.python.llm_agent_cd.workflow import role_llms
        frozen = role_llms(baseline)
        candidate["change_scope"] = "coordinator"
        candidate["llm_overrides"] = {
            role: frozen[role] for role in ROLES if role != "coordinator"
        }
    if digest(llm) != baseline["llm_version_id"]:
        host = "rec-llm-" + digest(llm)[:20] + "." + namespace + ".svc.cluster.local"
        changed_roles = ("coordinator",) if target_role == "coordinator" else ROLES
        for role in changed_roles:
            b = candidate["bindings"][role]
            b.update(managed_backend=True, backend_url="http://" + host + ":8000/v1", default_headers={})
            b.pop("health_url", None)
            b.pop("attestation_configmap", None)
            b["allowed_domains"] = sorted(set(b["allowed_domains"]) | {host})
    candidate = release(candidate)
    validate_experiment(baseline, candidate, c["experiment_type"])
    return candidate


def release_diff(a, b):
    if a.get("scope") == "workflow":
        return workflow_diff(a, b)
    return {
        "recommendation": {
            "before": a["config"],
            "after": b["config"],
            "changed": a["config_id"] != b["config_id"],
            "llm_changed": a["llm_version_id"] != b["llm_version_id"],
        }
    }


def jenkins_parameters(scope, config, candidate_uri, current_state_uri=None):
    item = target(scope)
    params = {
        "ACTION": "run",
        "EXPERIMENT_TYPE": config["experiment_type"],
        "CANDIDATE_MANIFEST": candidate_uri,
        "BASELINE_RELEASE_ID": config[baseline_field(scope)],
        "POLICY": item["policy_refs"][config["policy_ref"]],
    }
    if scope == "recommendation":
        params.update({
            "STATE_URI": current_state_uri or state_uri(scope),
            "FIXTURES": "configs/llm-ab/cases.json",
            "ROUTER_IMAGE": os.environ.get("AB_RECOMMENDATION_ROUTER_IMAGE")
            or os.environ["AB_DISPATCH_IMAGE"],
            "SOURCE_MODE": "deployed-image",
        })
    return params


def dispatch_job(experiment_id):
    scope = scope_from_experiment(experiment_id)
    if scope != "workflow":
        raise ValueError("Recommendation polling dispatches Jenkins directly")
    image = os.environ["AB_DISPATCH_IMAGE"]
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image):
        raise ValueError("dispatch image must be pinned")
    overlay = os.environ.get("AB_PROFILE_OVERLAY_CONFIGMAP", "")
    if overlay and not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", overlay):
        raise ValueError("invalid trigger source overlay")
    container = {"name": "dispatch", "image": image,
        "command": ["python", "-m", "apps.agentic.llm_ab_router.trigger", "dispatch", experiment_id],
        "envFrom": [{"secretRef": {"name": os.environ.get("AB_TRIGGER_SECRET", "recsys-workflow-trigger")}}],
        # Values injected into the receiver Deployment are not inherited by a
        # Kubernetes Job. Pin both images explicitly so the submitted Jenkins
        # parameters are reproducible and never fall back to a mutable tag.
        "env": [
            {"name": "AB_DISPATCH_IMAGE", "value": image},
            {"name": "AB_RECOMMENDATION_ROUTER_IMAGE", "value":
             os.environ.get("AB_RECOMMENDATION_ROUTER_IMAGE") or image},
        ],
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}}}
    pod = {"restartPolicy": "Never", "automountServiceAccountToken": False,
        "nodeSelector": {"recsys.ai/pool": "ml-system"},
        "tolerations": [{"key": "recsys.ai/workload", "operator": "Equal", "value": "ml-system", "effect": "NoSchedule"}],
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [container]}
    if overlay:
        container["volumeMounts"] = [
            {"name": "trigger-code", "mountPath": "/app/apps/agentic/llm_ab_router/trigger.py", "subPath": "trigger.py", "readOnly": True},
            {"name": "trigger-code", "mountPath": "/app/jenkins/python/llm_agent_cd/release.py", "subPath": "release.py", "readOnly": True},
            {"name": "trigger-code", "mountPath": "/app/jenkins/python/llm_agent_cd/workflow.py", "subPath": "workflow.py", "readOnly": True},
            {"name": "trigger-code", "mountPath": "/app/jenkins/python/llm_agent_cd/serving_profiles.py", "subPath": "serving_profiles.py", "readOnly": True},
        ]
        pod["volumes"] = [{"name": "trigger-code", "configMap": {"name": overlay}}]
    app = "recsys-workflow-dispatch"
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {
        "name": "ab-dispatch-" + experiment_id, "labels": {"app": app}},
        "spec": {"backoffLimit": 2, "activeDeadlineSeconds": 300, "ttlSecondsAfterFinished": 86400,
        "template": {"metadata": {"labels": {"app": app},
                                   "annotations": {"sidecar.istio.io/inject": "false"}},
        "spec": pod}}}


def dispatch_job_matches(existing, desired):
    """Kubernetes defaults are allowed; an unrelated Job collision is not success."""
    from jenkins.python.llm_agent_cd.driver import contains_spec
    if existing.get("metadata", {}).get("deletionTimestamp"):
        return False
    return contains_spec(existing.get("metadata", {}).get("labels"), desired["metadata"]["labels"]) and contains_spec(
        existing.get("spec"), desired["spec"])


class Trigger:
    def __init__(self):
        self.db = Database(os.environ["AB_DATABASE_URL"])
        self.http = httpx.Client(timeout=20, follow_redirects=False, transport=httpx.HTTPTransport(retries=0))

    def migrate(self):
        with self.db.connect() as c:
            c.execute("SELECT pg_advisory_lock(hashtextextended('recsys-ab-trigger-schema',0))")
            try:
                c.execute(ROUTER_DDL)
                c.execute(DDL)
            finally:
                c.execute("SELECT pg_advisory_unlock(hashtextextended('recsys-ab-trigger-schema',0))")

    def request(self, eid):
        with self.db.connect() as c:
            return c.execute("SELECT * FROM recsys_ab.trigger_requests WHERE experiment_id=%s", (eid,)).fetchone()

    def metrics(self):
        # Shared DB gauges: each receiver replica exports the same values. Never
        # sum replicas, and never rate() queue age or mutable dispatch status.
        with self.db.connect() as c:
            rows = c.execute("""SELECT experiment_id,status,extract(epoch FROM created_at) AS created,
                extract(epoch FROM updated_at) AS updated,
                CASE WHEN status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED')
                  THEN greatest(0,extract(epoch FROM now()-created_at)) END AS queue_age
                FROM recsys_ab.trigger_requests
                WHERE experiment_id LIKE %s ORDER BY created_at""",
                (configured_experiment_pattern(),)).fetchall()
            actions = c.execute("""SELECT experiment_id,action,target_weight,status,
                extract(epoch FROM updated_at) AS updated FROM recsys_ab.controller_actions
                ORDER BY created_at""").fetchall()
            traffic = c.execute("""SELECT experiment_id,status,phase,live_submitted,
                synthetic_submitted,extract(epoch FROM heartbeat_at) AS heartbeat
                FROM recsys_ab.traffic_runs ORDER BY created_at""").fetchall()
        lines = ["# TYPE recsys_workflow_dispatch_status gauge",
                 "# TYPE recsys_workflow_dispatch_queue_age_seconds gauge",
                 "# TYPE recsys_workflow_dispatch_updated_at gauge",
                 "# TYPE recsys_ab_controller_action gauge",
                 "# TYPE recsys_ab_controller_last_tick gauge",
                 "# TYPE recsys_ab_traffic_job_status gauge",
                 "# TYPE recsys_ab_traffic_job_heartbeat gauge",
                 "# TYPE recsys_ab_traffic_job_requests gauge"]
        for r in rows:
            labels = "experiment_id=" + json_label(r["experiment_id"]) + ",status=" + json_label(r["status"])
            lines.append("recsys_workflow_dispatch_status{" + labels + "} 1")
            lines.append("recsys_workflow_dispatch_updated_at{" + labels + "} " + str(r["updated"]))
            if r["queue_age"] is not None:
                lines.append("recsys_workflow_dispatch_queue_age_seconds{" + labels + "} " + str(r["queue_age"]))
        for row in actions:
            labels = ",".join([
                "experiment_id=" + json_label(row["experiment_id"]),
                "action=" + json_label(row["action"]),
                "target_weight=" + json_label(row["target_weight"] if row["target_weight"] is not None else "none"),
                "status=" + json_label(row["status"]),
            ])
            lines.append("recsys_ab_controller_action{" + labels + "} 1")
            lines.append("recsys_ab_controller_last_tick{experiment_id=" +
                         json_label(row["experiment_id"]) + "} " + str(row["updated"]))
        for row in traffic:
            labels = ",".join([
                "experiment_id=" + json_label(row["experiment_id"]),
                "status=" + json_label(row["status"]),
                "phase=" + json_label(row["phase"] or "WAITING"),
            ])
            lines.append("recsys_ab_traffic_job_status{" + labels + "} 1")
            if row["heartbeat"] is not None:
                lines.append("recsys_ab_traffic_job_heartbeat{experiment_id=" +
                             json_label(row["experiment_id"]) + "} " + str(row["heartbeat"]))
            for kind, field in (("live_test", "live_submitted"), ("synthetic_case", "synthetic_submitted")):
                lines.append("recsys_ab_traffic_job_requests{experiment_id=" +
                             json_label(row["experiment_id"]) + ",traffic_kind=" +
                             json_label(kind) + "} " + str(row[field]))
        return "\n".join(lines) + "\n"

    def update(self, eid, status, **values):
        allowed = {"reason", "candidate_uri", "candidate", "config_diff", "job_name", "queue_id", "build_url",
                   "label_state", "label_sync_status", "label_sync_error"}
        if set(values) - allowed:
            raise ValueError("invalid update")
        fields = ["status=%s", "updated_at=now()"] + [k + "=%s" for k in values]
        with self.db.connect() as c:
            c.execute("UPDATE recsys_ab.trigger_requests SET " + ",".join(fields) + " WHERE experiment_id=%s",
                      [status, *[Jsonb(v) if isinstance(v, dict) else v for v in values.values()], eid])
        # Retain the existing event name because dashboards and Loki queries
        # consume it for both workflow- and Recommendation-scoped rollouts.
        print(json.dumps({"event": "workflow.dispatch", "experiment_id": eid, "status": status,
                          "event_id": digest([eid, status, {k: v for k, v in values.items() if k in {"reason", "queue_id", "build_url", "job_name"}}]),
                          "at": time.time(),
                          **{k: v for k, v in values.items() if k in {"reason", "queue_id", "build_url", "job_name"}}}), flush=True)

    def prompt(self, name, version):
        response = self.http.get(os.environ["LANGFUSE_BASE_URL"].rstrip("/") + "/api/public/v2/prompts/" + quote(name, safe=""),
            params={"version": version}, auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]))
        response.raise_for_status()
        prompt = response.json()
        if (prompt.get("version") != version or prompt.get("name") != name
                or prompt.get("prompt") not in {PROMPT_TEXT, LEGACY_PROMPT_TEXT, PARKING_PROMPT_TEXT}):
            raise ValueError("unexpected prompt version or immutable prompt text changed")
        return prompt

    def ready_prompts(self):
        """List candidates, then require an exact-version read before use."""
        response = self.http.get(
            os.environ["LANGFUSE_BASE_URL"].rstrip("/") + "/api/public/v2/prompts",
            params={"name": os.environ["AB_LANGFUSE_PROMPT"], "label": READY_LABEL, "limit": 100},
            auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]),
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data", payload if isinstance(payload, list) else [])
        if not isinstance(rows, list):
            raise ValueError("invalid Langfuse prompt list response")
        versions = set()
        for row in rows:
            if row.get("name") != os.environ["AB_LANGFUSE_PROMPT"]:
                continue
            listed = row.get("versions", [row.get("version")])
            if not isinstance(listed, list):
                raise ValueError("invalid Langfuse prompt version list")
            versions.update(v for v in listed if type(v) is int and v > 0)
        return [self.prompt(os.environ["AB_LANGFUSE_PROMPT"], version)
                for version in sorted(versions)]

    def move_labels(self, name, version, labels):
        """Move pointer labels with the public API supported by Langfuse 4.17."""
        if (not labels or not all(isinstance(label, str) and re.fullmatch(r"[a-z0-9-]{1,64}", label)
                                  for label in labels)):
            raise ValueError("invalid Langfuse label update")
        response = self.http.patch(
            os.environ["LANGFUSE_BASE_URL"].rstrip("/") + "/api/public/v2/prompts/"
            + quote(name, safe="") + "/versions/" + str(version),
            json={"newLabels": labels},
            auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]),
        )
        response.raise_for_status()

    def parking_prompt(self):
        version = int(os.environ["AB_LANGFUSE_PARKING_VERSION"])
        prompt = self.prompt(os.environ["AB_LANGFUSE_PROMPT"], version)
        if (prompt.get("prompt") != PARKING_PROMPT_TEXT
                or prompt.get("config") != {"schema_version": 0, "scope": "recommendation",
                                             "kind": "ab-label-parking"}
                or PARKING_LABEL not in prompt.get("labels", [])):
            raise ValueError("Langfuse parking version identity mismatch")
        return prompt

    def _record_labels(self, eid, status, candidate, parking, error=None):
        state = {
            "candidate_version": candidate.get("version") if candidate else None,
            "candidate": sorted(candidate.get("labels", [])) if candidate else [],
            "parking_version": parking.get("version") if parking else None,
            "parking": sorted(parking.get("labels", [])) if parking else [],
        }
        self.update(eid, status, label_state=state,
                    label_sync_status="WAITING" if error else "SYNCED",
                    label_sync_error=error)
        with self.db.connect() as c:
            c.execute("UPDATE recsys_ab.trigger_requests SET labels_updated_at=now() WHERE experiment_id=%s", (eid,))

    def claim_labels(self, row, candidate):
        """Remove the ready pointer before Jenkins can receive the request."""
        eid = row["experiment_id"]
        parking = self.parking_prompt()
        labels = set(candidate.get("labels", []))
        parked = READY_LABEL in parking.get("labels", [])
        if ACTIVE_LABEL in labels and READY_LABEL not in labels and parked:
            self._record_labels(eid, row["status"], candidate, parking)
            return
        if READY_LABEL not in labels:
            raise ValueError("candidate withdrawn before label claim")
        self.move_labels(row["prompt_name"], row["prompt_version"], [ACTIVE_LABEL])
        self.move_labels(row["prompt_name"], parking["version"], [READY_LABEL])
        candidate = self.prompt(row["prompt_name"], row["prompt_version"])
        parking = self.parking_prompt()
        labels = set(candidate.get("labels", []))
        if ACTIVE_LABEL not in labels or READY_LABEL in labels or READY_LABEL not in parking.get("labels", []):
            raise ValueError("Langfuse label claim verification failed")
        self._record_labels(eid, row["status"], candidate, parking)

    def sync_terminal_labels(self):
        """Project durable terminal state to latest-result Langfuse pointers."""
        if not polling_mode():
            return
        with self.db.connect() as c:
            rows = c.execute("""SELECT * FROM recsys_ab.trigger_requests
                WHERE experiment_id LIKE 'rec-%%' AND status IN ('COMPLETED','REJECTED','ROLLED_BACK','ROLLBACK_FAILED')
                  AND coalesce(label_sync_status,'') <> 'TERMINAL_SYNCED'
                ORDER BY created_at LIMIT 100""").fetchall()
        for row in rows:
            try:
                parking = self.parking_prompt()
                label = TERMINAL_LABELS[row["status"]]
                self.move_labels(row["prompt_name"], row["prompt_version"], [label])
                # Both transient pointers must leave the candidate.  Langfuse
                # 4.17 moves each pointer when it is assigned to another version.
                self.move_labels(row["prompt_name"], parking["version"], [READY_LABEL, ACTIVE_LABEL])
                candidate = self.prompt(row["prompt_name"], row["prompt_version"])
                parking = self.parking_prompt()
                labels = set(candidate.get("labels", []))
                if label not in labels or labels & {READY_LABEL, ACTIVE_LABEL}:
                    raise ValueError("terminal Langfuse label verification failed")
                self._record_labels(row["experiment_id"], row["status"], candidate, parking)
                with self.db.connect() as c:
                    c.execute("UPDATE recsys_ab.trigger_requests SET label_sync_status='TERMINAL_SYNCED', label_sync_error=NULL, labels_updated_at=now() WHERE experiment_id=%s",
                              (row["experiment_id"],))
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                # Do not include response bodies or credentials in durable errors.
                self.update(row["experiment_id"], row["status"],
                            label_sync_status="WAITING", label_sync_error=type(exc).__name__)

    def accept(self, raw, signature):
        valid, timestamp = signature_valid(raw, signature, os.environ["LANGFUSE_WEBHOOK_SECRET"])
        if not valid:
            raise HTTPException(401, "invalid signature")
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise HTTPException(400, "webhook must be an object")
        if body.get("type") != "prompt-version" or body.get("apiVersion", "v1") != "v1":
            raise HTTPException(400, "unsupported webhook schema")
        event_id = body.get("id")
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
            raise HTTPException(400, "event ID required")
        checksum = hashlib.sha256(raw).hexdigest()
        with self.db.connect() as c:
            existing = c.execute("SELECT * FROM recsys_ab.trigger_events WHERE event_id=%s", (event_id,)).fetchone()
        if existing:
            if existing["payload_hash"] != checksum:
                raise HTTPException(409, "event ID collision")
            return {"experiment_id": existing["experiment_id"], "duplicate": True}
        if abs(time.time() - timestamp) > 300:
            raise HTTPException(401, "expired event")
        p = body.get("prompt", {})
        if not isinstance(p, dict):
            raise HTTPException(400, "prompt must be an object")
        if not isinstance(p.get("labels", []), list) or not all(isinstance(label, str) for label in p.get("labels", [])):
            raise HTTPException(400, "labels must be an array of strings")
        if p.get("projectId") != os.environ["LANGFUSE_PROJECT_ID"] or p.get("name") != os.environ["AB_LANGFUSE_PROMPT"]:
            raise HTTPException(403, "prompt/project not allowed")
        if body.get("action") not in {"created", "updated"} or "ab-ready" not in p.get("labels", []):
            return {"ignored": True}
        if type(p.get("version")) is not int or p["version"] < 1:
            raise HTTPException(400, "positive prompt version required")
        prompt = self.prompt(p["name"], p["version"])
        if "ab-ready" not in prompt.get("labels", []):
            return {"ignored": True}
        config = validate_config(prompt["config"])
        configured_scope = os.environ.get("AB_TRIGGER_SCOPE")
        if configured_scope and config["scope"] != configured_scope:
            raise HTTPException(403, "prompt scope is not allowed by this receiver")
        scope = config["scope"]
        immutable_checksum = prompt_checksum(p["projectId"], prompt)
        eid = target(scope)["prefix"] + "-" + digest([p["projectId"], p["name"], p["version"]])[:32]
        with self.db.connect() as c, c.transaction():
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (scope + "-webhook:" + event_id,))
            existing = c.execute("SELECT * FROM recsys_ab.trigger_events WHERE event_id=%s", (event_id,)).fetchone()
            if existing and existing["payload_hash"] != checksum:
                raise HTTPException(409, "event ID collision")
            c.execute("""INSERT INTO recsys_ab.trigger_requests(
                experiment_id,project_id,prompt_name,prompt_version,prompt_checksum,config,prompt_snapshot,label_state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                      (eid, p["projectId"], p["name"], p["version"], immutable_checksum,
                       Jsonb(config), Jsonb(prompt), Jsonb({"candidate": sorted(prompt.get("labels", []))})))
            stored = c.execute("SELECT config,prompt_checksum FROM recsys_ab.trigger_requests WHERE experiment_id=%s", (eid,)).fetchone()
            if (not stored or stored["config"] != config
                    or stored.get("prompt_checksum") not in {None, immutable_checksum}):
                raise HTTPException(409, "immutable prompt version collision")
            c.execute("INSERT INTO recsys_ab.trigger_events(event_id,payload_hash,experiment_id,payload) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                      (event_id, checksum, eid, Jsonb(body)))
        # Inbox is already durable. The recovery CronJob also creates missing Jobs.
        try:
            self.ensure_job(eid)
        except httpx.HTTPError:
            pass
        return {"experiment_id": eid, "accepted": True}

    def _catalog_and_candidate(self, state, config):
        scope = config["scope"]
        ref = config["llm_release_ref"]
        if ref == state["champion"]["llm_version_id"]:
            llm = deepcopy(state["champion"]["llm"])
        else:
            client = s3_client()
            llm = json.loads(client.get_object(
                Bucket=os.environ.get("AB_CATALOG_BUCKET", "recsys-llm-ab"),
                Key=target(scope)["catalog_prefix"] + ref + ".json")["Body"].read())
            if scope == "recommendation":
                try:
                    attestation = json.loads(client.get_object(
                        Bucket=os.environ.get("AB_CATALOG_BUCKET", "recsys-llm-ab"),
                        Key="recommendation/catalog-attestations/" + ref + ".json",
                    )["Body"].read())
                except (ClientError, KeyError) as exc:
                    raise ValueError("catalog lacks model-onboarding attestation") from exc
                if (attestation.get("llm_release_ref") != ref
                        or attestation.get("scope") != "recommendation"
                        or attestation.get("status") not in {"READY", "legacy-approved"}):
                    raise ValueError("catalog onboarding attestation is not runnable")
        if digest(llm) != ref:
            raise ValueError("catalog object digest mismatch")
        candidate = candidate_from_config(
            state["champion"], config, llm, os.environ.get("AB_NAMESPACE", "kagent")
        )
        return llm, candidate

    @staticmethod
    def candidate_rejection(state, candidate):
        if (
            candidate.get("scope", "recommendation") != "recommendation"
            and candidate["release_id"] in state.get("disabled", [])
        ):
            return "candidate release is quarantined"
        return None

    def persist_polled_prompt(self, prompt):
        """Validate and durably snapshot an exact ready version once."""
        project_id = os.environ["LANGFUSE_PROJECT_ID"]
        if (prompt.get("name") != os.environ["AB_LANGFUSE_PROMPT"]
                or prompt.get("prompt") != PROMPT_TEXT
                or READY_LABEL not in prompt.get("labels", [])):
            raise ValueError("ready prompt identity mismatch")
        config = validate_config(prompt.get("config"))
        if config["scope"] != "recommendation":
            raise ValueError("poller only accepts Recommendation requests")
        checksum = prompt_checksum(project_id, prompt)
        eid = "rec-" + digest([project_id, prompt["name"], prompt["version"], checksum])[:32]
        state, _ = StateStore(state_uri("recommendation")).read()
        if state["phase"] not in {"IDLE", "COMPLETED", "ROLLED_BACK"}:
            return None
        if state.get("cleanup") and state["cleanup"].get("status") != "CLEANED":
            return None
        _, candidate = self._catalog_and_candidate(state, config)
        rejection = self.candidate_rejection(state, candidate)
        initial_status = "REJECTED" if rejection else "QUEUED"
        with self.db.connect() as c, c.transaction():
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                      ("recommendation-prompt:" + prompt["name"] + ":" + str(prompt["version"]),))
            existing_version = c.execute("""SELECT experiment_id,prompt_checksum FROM recsys_ab.trigger_requests
                WHERE prompt_name=%s AND prompt_version=%s""",
                (prompt["name"], prompt["version"])).fetchone()
            if existing_version and existing_version.get("prompt_checksum") not in {None, checksum}:
                raise ValueError("immutable prompt version checksum collision")
            c.execute("""INSERT INTO recsys_ab.trigger_requests(
                experiment_id,project_id,prompt_name,prompt_version,prompt_checksum,config,prompt_snapshot,
                status,reason,candidate,config_diff,label_state,label_sync_status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'DISCOVERED') ON CONFLICT DO NOTHING""",
                (eid, project_id, prompt["name"], prompt["version"], checksum,
                 Jsonb(config), Jsonb(canonical_prompt_snapshot(project_id, prompt)),
                 initial_status, rejection, Jsonb(candidate),
                 Jsonb(release_diff(state["champion"], candidate)),
                 Jsonb({"candidate_version": prompt["version"],
                        "candidate": sorted(prompt.get("labels", []))})))
            stored = c.execute("SELECT * FROM recsys_ab.trigger_requests WHERE experiment_id=%s", (eid,)).fetchone()
            if (not stored or stored["project_id"] != project_id or stored["prompt_checksum"] != checksum
                    or stored["config"] != config):
                raise ValueError("immutable polled prompt collision")
        return eid

    def ensure_job(self, eid):
        if os.environ.get("AB_DISPATCH_ENABLED") != "true":
            return
        obj = dispatch_job(eid)
        token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
        import ssl
        tls = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        with httpx.Client(verify=tls, timeout=15) as kube:
            response = kube.post("https://kubernetes.default.svc/apis/batch/v1/namespaces/" + os.environ.get("AB_NAMESPACE", "kagent") + "/jobs",
                                 json=obj, headers={"Authorization": "Bearer " + token})
            if response.status_code == 409:
                existing = kube.get("https://kubernetes.default.svc/apis/batch/v1/namespaces/" + os.environ.get("AB_NAMESPACE", "kagent") + "/jobs/" + obj["metadata"]["name"],
                                    headers={"Authorization": "Bearer " + token})
                existing.raise_for_status()
                if not dispatch_job_matches(existing.json(), obj):
                    self.update(eid, "NEEDS_ATTENTION", reason="dispatch Job name occupied by different spec")
                    return
            else:
                response.raise_for_status()
        with self.db.connect() as c:
            c.execute("UPDATE recsys_ab.trigger_requests SET job_name=%s WHERE experiment_id=%s", (obj["metadata"]["name"], eid))

    def jenkins(self, path, method="GET", **kwargs):
        base = os.environ["AB_JENKINS_URL"].rstrip("/")
        response = self.http.request(method, base + path, auth=(os.environ["AB_JENKINS_USER"], os.environ["AB_JENKINS_TOKEN"]), **kwargs)
        response.raise_for_status()
        return response

    def reconcile_build(self, row):
        eid = row["experiment_id"]
        scope = scope_from_experiment(eid)
        job_name = jenkins_job(scope)
        job = quote(job_name, safe="")
        builds = self.jenkins("/job/" + job + "/api/json", params={"tree": "builds[number,url,building,result,actions[parameters[name,value]]]{0,100}"}).json().get("builds", [])
        queued = self.jenkins("/queue/api/json", params={"tree": "items[id,task[name],actions[parameters[name,value]]]"}).json().get("items", [])
        def matches(entry):
            return any(p.get("name") == "EXPERIMENT_ID" and p.get("value") == eid
                       for a in entry.get("actions", []) for p in a.get("parameters", []))
        found = [b for b in builds if matches(b)]
        queued = [q for q in queued if q.get("task", {}).get("name") == job_name and matches(q)]
        if len(found) + len(queued) > 1:
            self.update(eid, "NEEDS_ATTENTION", reason="multiple Jenkins deliveries found; operator reconciliation required")
            return True
        if found:
            build = found[0]
            store = StateStore(state_uri(scope))
            state, _ = store.read()
            if state.get("experiment_id") != eid:
                archived = next((r for r in reversed(state.get("history", [])) if r["experiment_id"] == eid), None)
                if archived:
                    state = store.read_archive(archived)
            if build.get("building"):
                status = "RUNNING"
            elif state.get("experiment_id") == eid and state["phase"] in {
                    "COMPLETED", "ROLLED_BACK", "ROLLBACK_FAILED",
                    "HOLD", "WAITING", "NEEDS_ATTENTION"}:
                status = state["phase"]
            elif (build.get("result") in {"FAILURE", "ABORTED", "NOT_BUILT"}
                    and (row.get("candidate") or {}).get("release_id")
                    in state.get("disabled", [])):
                # Jenkins may reject an already-quarantined immutable release
                # before Engine.start writes this experiment into state.  That
                # outcome is deterministic, not an uncertain delivery, and can
                # safely project ab-fail without any inference replay.
                status = "REJECTED"
            else:
                status = "NEEDS_ATTENTION"
            values = {"build_url": build["url"]}
            if status == "REJECTED":
                values["reason"] = "candidate release is quarantined; Jenkins rejected before experiment start"
            elif status != "NEEDS_ATTENTION":
                values["reason"] = None
            self.update(eid, status, **values)
            return True
        if queued:
            self.update(eid, "SUBMITTED", queue_id=str(queued[0]["id"]))
            return True
        return False

    def dispatch(self, eid):
        if os.environ.get("AB_DISPATCH_ENABLED") != "true":
            return
        scope = scope_from_experiment(eid)
        with self.db.session(scope + "-trigger:" + eid):
            return self._dispatch(eid)

    def _dispatch(self, eid):
        row = self.request(eid)
        if not row or row["status"] not in {"QUEUED", "WAITING", "DISPATCHING", "SUBMITTED", "RUNNING"}:
            return
        if row["status"] in {"DISPATCHING", "SUBMITTED", "RUNNING"}:
            if row["submitted_at"] and time.time() - row["submitted_at"].timestamp() < 60:
                return
            if not self.reconcile_build(row):
                self.update(eid, "NEEDS_ATTENTION", reason="uncertain Jenkins delivery; never auto-resubmit")
            return
        scope = scope_from_experiment(eid)
        if row["config"].get("scope") != scope:
            self.update(eid, "REJECTED", reason="experiment/config scope mismatch")
            return
        if polling_mode():
            with self.db.connect() as c:
                active = c.execute("""SELECT experiment_id FROM recsys_ab.trigger_requests
                    WHERE experiment_id LIKE 'rec-%%' AND (
                      status IN ('DISPATCHING','SUBMITTED','RUNNING','HOLD','NEEDS_ATTENTION')
                      OR (status='WAITING' AND label_state->'candidate' ? 'ab-running'))
                      AND experiment_id<>%s LIMIT 1""", (eid,)).fetchone()
            if active:
                self.update(eid, "WAITING", reason="another Recommendation experiment is active")
                return
        item = target(scope)
        current_state_uri = state_uri(scope)
        state, _ = StateStore(current_state_uri).read()
        if state["phase"] not in {"IDLE", "COMPLETED", "ROLLED_BACK"}:
            self.update(eid, "WAITING", reason="another experiment is active")
            return
        try:
            prompt = self.prompt(row["prompt_name"], row["prompt_version"])
            labels = set(prompt.get("labels", []))
            allowed_label_state = (READY_LABEL in labels or
                                   (polling_mode() and ACTIVE_LABEL in labels and READY_LABEL not in labels))
            if not allowed_label_state or prompt["config"] != row["config"]:
                raise ValueError("candidate withdrawn or changed")
            if row.get("prompt_checksum") and prompt_checksum(
                    row.get("project_id") or os.environ["LANGFUSE_PROJECT_ID"], prompt
                    ) != row["prompt_checksum"]:
                raise ValueError("immutable prompt checksum mismatch")
            _, candidate = self._catalog_and_candidate(state, row["config"])
            rejection = self.candidate_rejection(state, candidate)
            if rejection:
                raise ValueError(rejection)
            bucket = os.environ.get("AB_CATALOG_BUCKET", "recsys-llm-ab")
            key = item["candidate_prefix"] + eid + ".json"
            data = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
            from botocore.exceptions import ClientError
            try:
                s3_client().put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/json", IfNoneMatch="*")
            except ClientError as exc:
                if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                    raise
                if s3_client().get_object(Bucket=bucket, Key=key)["Body"].read() != data:
                    raise ValueError("immutable candidate collision")
            self.update(eid, "QUEUED", candidate_uri="s3://" + bucket + "/" + key,
                        candidate=candidate, config_diff=release_diff(state["champion"], candidate))
            if polling_mode():
                self.claim_labels(self.request(eid), prompt)
        except ValueError as exc:
            self.update(eid, "REJECTED", reason=str(exc))
            return
        except httpx.HTTPError as exc:
            # Label movement is part of the durable claim.  A failed claim can
            # be retried safely next minute and must never reach Jenkins.
            self.update(eid, "WAITING", reason="Langfuse label claim pending",
                        label_sync_status="WAITING", label_sync_error=type(exc).__name__)
            return
        if polling_mode():
            # Recommendation is controller-driven: this tick registers and
            # dispatches only the prepare executor action. Workflow keeps the
            # legacy long-running delivery path below.
            from .controller import RecommendationController

            return RecommendationController(self).tick(eid)
        # A previous ambiguous submit may already exist even if local DB state
        # was lost before the queue/build identifiers were stored.
        if self.reconcile_build(self.request(eid)):
            return
        with self.db.connect() as c, c.transaction():
            # Serialize all dispatchers, including duplicate Job pods.
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended('recsys-release-dispatch',0))")
            active = c.execute("SELECT experiment_id FROM recsys_ab.trigger_requests WHERE status IN ('DISPATCHING','SUBMITTED','RUNNING') AND experiment_id<>%s", (eid,)).fetchone()
            if active:
                c.execute("UPDATE recsys_ab.trigger_requests SET status='WAITING' WHERE experiment_id=%s AND status='QUEUED'", (eid,))
                return
            changed = c.execute("UPDATE recsys_ab.trigger_requests SET status='DISPATCHING',submitted_at=now() WHERE experiment_id=%s AND status IN ('QUEUED','WAITING') RETURNING experiment_id", (eid,)).fetchone()
            if not changed:
                return
        job = quote(jenkins_job(scope), safe="")
        params = jenkins_parameters(
            scope, row["config"], "s3://" + bucket + "/" + key, current_state_uri
        )
        params["EXPERIMENT_ID"] = eid
        try:
            response = self.jenkins("/job/" + job + "/buildWithParameters", method="POST", data=params)
            self.update(eid, "SUBMITTED", queue_id=response.headers.get("location", "").rstrip("/").split("/")[-1])
        except httpx.HTTPError:
            self.update(eid, "DISPATCHING", reason="submission response uncertain; reconcile only")

    def reconcile(self):
        with self.db.connect() as c:
            rows = c.execute("""SELECT * FROM recsys_ab.trigger_requests
                WHERE experiment_id LIKE %s AND (
                  status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED','RUNNING')
                  OR (status='NEEDS_ATTENTION' AND build_url IS NOT NULL))
                ORDER BY created_at LIMIT 100""",
                (configured_experiment_pattern(),)).fetchall()
        for row in rows:
            if row["status"] == "NEEDS_ATTENTION":
                # An operator may have rolled back after an interrupted build.
                # Refresh only known delivery evidence, never dispatch/replay.
                scope = scope_from_experiment(row["experiment_id"])
                with self.db.session(scope + "-trigger:" + row["experiment_id"]):
                    current = self.request(row["experiment_id"])
                    if current and current["status"] == "NEEDS_ATTENTION" and current.get("build_url"):
                        self.reconcile_build(current)
                continue
            if row["status"] in {"QUEUED", "WAITING"}:
                if polling_mode():
                    self.dispatch(row["experiment_id"])
                    continue
                if not row.get("job_name"):
                    # An ACKed event whose initial Kubernetes POST was lost must
                    # still get its deterministic candidate Job before dispatch.
                    self.ensure_job(row["experiment_id"])
                    continue
                # Finite recovery Job may dispatch a waiting request after its original Job finished.
                self.dispatch(row["experiment_id"])
            elif row["submitted_at"] and time.time() - row["submitted_at"].timestamp() > 60:
                self.dispatch(row["experiment_id"])

    def reconcile_model_onboardings(self):
        """Retire unused 0%-traffic candidates after the reviewed two-hour TTL."""
        if not polling_mode() or os.environ.get("AB_DISPATCH_ENABLED") != "true":
            return
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM recsys_ab.model_onboardings
                   WHERE (status='READY' AND expires_at<=now())
                      OR (status='EXPIRED' AND cleanup_requested_at IS NOT NULL)
                   ORDER BY expires_at LIMIT 10"""
            ).fetchall()
        state, _ = StateStore(state_uri("recommendation")).read()
        protected = {item.get("release_id") for item in
                     (state.get("champion"), state.get("previous")) if item}
        if state.get("phase") not in {"IDLE", "COMPLETED", "ROLLED_BACK"} and state.get("pending"):
            protected.add(state["pending"]["release_id"])
        job_name = os.environ.get("AB_ONBOARDING_JENKINS_JOB", "RecSys-LLM-Candidate-Onboard")
        job = quote(job_name, safe="")

        def matches(entry, onboarding_id):
            values = {p.get("name"): p.get("value") for action in entry.get("actions", [])
                      for p in action.get("parameters", [])}
            return values.get("ONBOARDING_ID") == onboarding_id and values.get("ACTION") == "cleanup"

        for row in rows:
            with self.db.connect() as connection:
                active = connection.execute(
                    """SELECT 1 FROM recsys_ab.trigger_requests
                       WHERE candidate->>'release_id'=%s
                         AND status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED','RUNNING','HOLD','NEEDS_ATTENTION')
                       LIMIT 1""",
                    (row["candidate_release_id"],),
                ).fetchone()
            if active or row["candidate_release_id"] in protected:
                continue  # TTL is suspended while a release has a live owner.
            builds = self.jenkins(
                "/job/" + job + "/api/json",
                params={"tree": "builds[number,url,building,result,actions[parameters[name,value]]]{0,100}"},
            ).json().get("builds", [])
            queued = self.jenkins(
                "/queue/api/json",
                params={"tree": "items[id,task[name],actions[parameters[name,value]]]"},
            ).json().get("items", [])
            found = [entry for entry in builds if matches(entry, row["onboarding_id"])]
            waiting = [entry for entry in queued if entry.get("task", {}).get("name") == job_name
                       and matches(entry, row["onboarding_id"])]
            if len(found) + len(waiting) > 1:
                with self.db.connect() as connection:
                    connection.execute(
                        """UPDATE recsys_ab.model_onboardings SET status='BLOCKED',
                           reason='multiple cleanup deliveries found',updated_at=now()
                           WHERE onboarding_id=%s""", (row["onboarding_id"],))
                continue
            if found or waiting:
                continue
            if row["cleanup_requested_at"] is not None:
                # A recorded submit intent with no observable delivery is
                # ambiguous. Never create a second cleanup build blindly.
                continue
            with self.db.connect() as connection:
                changed = connection.execute(
                    """UPDATE recsys_ab.model_onboardings
                       SET status='EXPIRED',cleanup_requested_at=now(),updated_at=now()
                       WHERE onboarding_id=%s AND status='READY' AND cleanup_requested_at IS NULL
                       RETURNING onboarding_id""", (row["onboarding_id"],)).fetchone()
            if not changed:
                continue
            params = {
                "ACTION": "cleanup",
                "ONBOARDING_ID": row["onboarding_id"],
                "INTENT_URI": row["intent_uri"],
                "ROUTER_IMAGE": os.environ["AB_RECOMMENDATION_ROUTER_IMAGE"],
            }
            try:
                self.jenkins("/job/" + job + "/buildWithParameters", method="POST", data=params)
            except httpx.HTTPError:
                # Next tick reconciles queue/build history only. A human can
                # resolve a truly lost delivery without risking double action.
                pass

    def poll(self):
        if not polling_mode():
            raise ValueError("poll action is restricted to Recommendation")
        with self.db.session("recommendation-langfuse-poller"):
            # Recovery is always first. Terminal label sync is delivery-only
            # and never creates a Jenkins build or model invocation.
            from .controller import RecommendationController

            controller = RecommendationController(self)
            controller.reconcile_actions()
            self.sync_terminal_labels()
            self.reconcile_model_onboardings()
            with self.db.connect() as c:
                active = c.execute("""SELECT experiment_id FROM recsys_ab.trigger_requests
                    WHERE experiment_id LIKE 'rec-%%' AND (
                      status IN ('DISPATCHING','SUBMITTED','RUNNING','HOLD','NEEDS_ATTENTION')
                      OR (status IN ('QUEUED','WAITING') AND label_state->'candidate' ? 'ab-running'))
                    LIMIT 1""").fetchone()
            state, _ = StateStore(state_uri("recommendation")).read()
            if active:
                result = controller.tick(active["experiment_id"])
                self.db.record_controller_tick(
                    active["experiment_id"], result.get("action") or result["status"],
                    result.get("reason"),
                )
                self.sync_terminal_labels()
                return result
            cleanup_pending = (
                state.get("cleanup")
                and state["cleanup"].get("status") != "CLEANED"
            )
            if (
                cleanup_pending
                or state["phase"] not in {"IDLE", "COMPLETED", "ROLLED_BACK"}
            ):
                eid = state.get("experiment_id")
                row = self.request(eid) if eid else None
                if row:
                    result = controller.tick(eid)
                    self.db.record_controller_tick(
                        eid, result.get("action") or result["status"], result.get("reason")
                    )
                    self.sync_terminal_labels()
                    return result
                return {"status": "BUSY", "experiment_id": eid}
            parking_version = int(os.environ["AB_LANGFUSE_PARKING_VERSION"])
            accepted = []
            dispatched = None
            for prompt in self.ready_prompts():
                if prompt.get("version") == parking_version:
                    continue
                eid = self.persist_polled_prompt(prompt)
                if not eid:
                    break
                accepted.append(eid)
                dispatched = self.dispatch(eid)
                if isinstance(dispatched, dict):
                    self.db.record_controller_tick(
                        eid, dispatched.get("action") or dispatched.get("status", "prepare"),
                        dispatched.get("reason"),
                    )
                # A label is a single pointer today, but stop after one exact
                # version even if a future API returns multiple candidates.
                break
            self.sync_terminal_labels()
            return dispatched or {"status": "DISPATCHED" if accepted else "IDLE",
                                  "experiment_id": accepted[0] if accepted else None}


def create_app(service=None):
    service = service or Trigger()
    @asynccontextmanager
    async def lifespan(app):
        service.migrate()
        yield
    app = FastAPI(lifespan=lifespan)
    @app.get("/healthz")
    def health():
        with service.db.connect() as c:
            c.execute("SELECT 1")
        return {"ok": True}
    @app.post("/webhooks/langfuse", status_code=202)
    @app.post("/webhooks/langfuse-recommendation", status_code=202)
    async def webhook(request: Request):
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise HTTPException(400, "invalid content length")
        if size > 262144:
            raise HTTPException(413)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 262144:
                raise HTTPException(413)
        raw = bytes(raw)
        import asyncio
        try:
            return await asyncio.to_thread(service.accept, raw, request.headers.get("x-langfuse-signature", ""))
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "invalid agent rollout webhook")
    @app.get("/metrics")
    def metrics():
        return Response(service.metrics(), media_type="text/plain; version=0.0.4")
    @app.get("/experiments/{eid}")
    def status(eid: str, request: Request):
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + os.environ["AB_TRIGGER_STATUS_TOKEN"]):
            raise HTTPException(403)
        row = service.request(eid)
        if not row:
            raise HTTPException(404)
        return {k: row[k] for k in ("experiment_id", "status", "reason", "job_name", "queue_id", "build_url", "config_diff", "created_at", "updated_at")}
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["serve", "dispatch", "reconcile", "poll", "migrate"])
    parser.add_argument("experiment_id", nargs="?")
    args = parser.parse_args()
    if args.action == "serve":
        import uvicorn
        uvicorn.run(create_app(), host="0.0.0.0", port=8080)
    else:
        service = Trigger()
        service.migrate()
        if args.action == "migrate":
            print(json.dumps({"status": "MIGRATED"}))
        elif args.action == "dispatch":
            service.dispatch(args.experiment_id)
        elif args.action == "poll":
            print(json.dumps(service.poll(), sort_keys=True))
        else:
            service.reconcile()

if __name__ == "__main__":
    main()
