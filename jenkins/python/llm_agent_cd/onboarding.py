"""Jenkins-side orchestration for zero-traffic Recommendation model onboarding."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import time

from psycopg.types.json import Jsonb

from apps.agentic.llm_ab_router.database import Database
from jenkins.python.model_cd.storage import parse_s3_uri, s3_client

from .capacity import quantity, verify_capacity
from .driver import Driver
from .manifests import backend_resources, resources
from .model_onboarding import canonical_bytes, load_policy
from .onboarding_compatibility import CASES, job as compatibility_job
from .release import digest, release, validate_experiment
from .state import StateStore


BUCKET = "recsys-llm-ab"
TERMINAL = {"READY", "BLOCKED", "EXPIRED", "CLEANED"}


def _object(client, key):
    return json.loads(client.get_object(Bucket=BUCKET, Key=key)["Body"].read())


def _put_once(client, key, value):
    from botocore.exceptions import ClientError

    body = canonical_bytes(value)
    try:
        client.put_object(Bucket=BUCKET, Key=key, Body=body,
                          ContentType="application/json", IfNoneMatch="*")
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
            raise
        if client.get_object(Bucket=BUCKET, Key=key)["Body"].read() != body:
            raise ValueError("immutable onboarding object collision") from exc
    return body


class Repository:
    """Small persistence boundary for the additive onboarding schema."""

    def __init__(self, db: Database):
        self.db = db

    def ensure(self, intent: dict, intent_uri: str):
        artifact = intent["artifact"]
        with self.db.connect() as connection, connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                               ("model-onboarding:" + intent["model_alias"],))
            connection.execute(
                """INSERT INTO recsys_ab.model_onboardings(
                     onboarding_id,scope,model_alias,artifact_identity,policy_checksum,
                     llm_release_ref,candidate_release_id,intent_uri,status)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'DISCOVERED')
                   ON CONFLICT DO NOTHING""",
                (intent["onboarding_id"], intent["scope"], intent["model_alias"],
                 Jsonb(artifact), intent["policy_checksum"], intent["llm_release_ref"],
                 intent["candidate"]["release_id"], intent_uri),
            )
            row = connection.execute(
                "SELECT * FROM recsys_ab.model_onboardings WHERE scope=%s AND model_alias=%s",
                (intent["scope"], intent["model_alias"]),
            ).fetchone()
        expected = {
            "onboarding_id": intent["onboarding_id"],
            "artifact_identity": artifact,
            "policy_checksum": intent["policy_checksum"],
            "llm_release_ref": intent["llm_release_ref"],
            "candidate_release_id": intent["candidate"]["release_id"],
            "intent_uri": intent_uri,
        }
        if not row or any(row[key] != value for key, value in expected.items()):
            raise ValueError("model alias already belongs to a different immutable identity")
        return row

    def mark(self, onboarding_id: str, status: str, **values):
        allowed = {"reason", "build_url", "evidence_uri", "prepared_at", "expires_at",
                   "cleanup_requested_at", "cleaned_at"}
        if set(values) - allowed:
            raise ValueError("invalid onboarding update")
        fields = ["status=%s", "updated_at=now()"] + [key + "=%s" for key in values]
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE recsys_ab.model_onboardings SET " + ",".join(fields) +
                " WHERE onboarding_id=%s",
                [status, *values.values(), onboarding_id],
            )

    def compatibility(self, onboarding_id: str):
        with self.db.connect() as connection:
            return connection.execute(
                """SELECT case_id,status,result,latency_seconds,finished_at
                   FROM recsys_ab.onboarding_compatibility
                   WHERE onboarding_id=%s ORDER BY case_id""",
                (onboarding_id,),
            ).fetchall()


def validate_intent(intent: dict, policy: dict | None = None):
    required = {"schema_version", "onboarding_id", "scope", "model_alias", "artifact",
                "profile", "catalog", "llm_release_ref", "candidate", "expected_champion",
                "policy_checksum", "created_at"}
    if set(intent) != required or intent["schema_version"] != 1:
        raise ValueError("invalid onboarding intent schema")
    if policy is not None and intent["policy_checksum"] != digest(policy):
        raise ValueError("onboarding policy changed after discovery")
    if digest(intent["catalog"]) != intent["llm_release_ref"]:
        raise ValueError("onboarding catalog identity mismatch")
    candidate = release(intent["candidate"])
    if candidate["release_id"] != intent["candidate"]["release_id"]:
        raise ValueError("candidate release identity mismatch")
    if candidate["llm"] != intent["catalog"]:
        raise ValueError("candidate does not bind the discovered catalog")
    return candidate


def _deployment_capacity(driver: Driver, candidate: dict):
    desired = [item for item in
               backend_resources(candidate, driver.namespace, driver.image)
               + resources(candidate, driver.namespace, driver.image, driver.secret)
               if item["kind"] == "Deployment"]
    nodes = json.loads(driver.kube("get", "nodes", "-o", "json"))["items"]
    pods = json.loads(driver.kube("get", "pods", "-A", "-o", "json"))["items"]
    current = json.loads(driver.kube("get", "deployments", "-A", "-o", "json"))["items"]
    existing = {(item["metadata"]["namespace"], item["metadata"]["name"]): item for item in current}
    free = verify_capacity(nodes, pods, desired, existing,
                           headroom={"cpu": "200m", "memory": "128Mi"})
    return {node: {key: str(value) for key, value in values.items()} for node, values in free.items()}


def _pod_memory(text: str):
    fields = text.split()
    if len(fields) < 3:
        raise ValueError("metrics-server returned an invalid pod sample")
    return quantity(fields[2])


def _wait_compatibility(driver: Driver, job_name: str, backend: str, timeout=900):
    deadline, peak = time.monotonic() + timeout, 0
    metrics_deadline = None
    while time.monotonic() < deadline:
        raw = json.loads(driver.kube("get", "job", job_name, "-o", "json"))
        try:
            sample = driver.kube("top", "pod", "-l", "app=" + backend,
                                 "--no-headers").strip()
            if sample:
                peak = max(peak, max(_pod_memory(line) for line in sample.splitlines()))
        except RuntimeError:
            # Metrics readiness is allowed to lag startup, but evidence is
            # mandatory before a successful return.
            pass
        if raw.get("status", {}).get("succeeded") == 1:
            if peak:
                return peak
            # A short compatibility suite can finish before metrics-server's
            # first scrape of a newly created backend.  Keep the gate strict,
            # but give telemetry one bounded scrape window after completion.
            metrics_deadline = metrics_deadline or min(
                deadline, time.monotonic() + 60
            )
            if time.monotonic() >= metrics_deadline:
                raise ValueError("working-set memory evidence is unavailable")
        if raw.get("status", {}).get("failed"):
            raise ValueError("onboarding compatibility Job failed")
        time.sleep(2)
    raise TimeoutError("onboarding compatibility Job timed out")


def _node_memory_headroom(driver: Driver, backend: str):
    pods = json.loads(driver.kube("get", "pods", "-l", "app=" + backend, "-o", "json"))["items"]
    ready = [pod for pod in pods if pod.get("spec", {}).get("nodeName")]
    if len(ready) != 1:
        raise ValueError("managed backend must have one scheduled pod")
    node = ready[0]["spec"]["nodeName"]
    allocatable = json.loads(driver.kube("get", "node", node, "-o", "json"))["status"]["allocatable"]["memory"]
    line = driver.kube("top", "node", node, "--no-headers").strip()
    fields = line.split()
    if len(fields) < 4:
        raise ValueError("metrics-server returned an invalid node sample")
    used = quantity(fields[3])
    remaining_ratio = float((quantity(allocatable) - used) / quantity(allocatable))
    if remaining_ratio < 0.20:
        raise ValueError("node has less than 20% actual RAM available")
    return {"node": node, "remaining_ratio": remaining_ratio}


def _percentile(values, quantile):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


class Onboarding:
    def __init__(self, driver=None, db=None, client=None, clock=time.time):
        self.driver = driver or Driver()
        self.db = db or self.driver.db
        self.client = client or s3_client()
        self.repo = Repository(self.db)
        self.clock = clock

    def _load(self, uri):
        bucket, key = parse_s3_uri(uri)
        if bucket != BUCKET or not key.startswith("recommendation/onboarding/"):
            raise ValueError("onboarding intent is outside the managed prefix")
        return _object(self.client, key)

    def _state(self):
        uri = os.environ.get("AB_RECOMMENDATION_STATE_URI") or os.environ["AB_STATE_URI"]
        return StateStore(uri).read()[0]

    def _publish(self, intent, evidence):
        ref, alias = intent["llm_release_ref"], intent["model_alias"]
        aliases = self.client.list_objects_v2(
            Bucket=BUCKET, Prefix="recommendation/aliases/"
        ).get("Contents", [])
        for item in aliases:
            existing = _object(self.client, item["Key"])
            if existing.get("llm_release_ref") == ref and existing.get("model_alias") != alias:
                raise ValueError("LLM release already has a primary alias")
        keys = {
            "profile": "recommendation/profiles/" + intent["profile"]["profileId"] + ".json",
            "catalog": "recommendation/catalog/" + ref + ".json",
            "alias": "recommendation/aliases/" + alias + ".json",
            "attestation": "recommendation/catalog-attestations/" + ref + ".json",
            "evidence": "recommendation/onboarding/" + intent["onboarding_id"] + "/evidence.json",
        }
        alias_record = {"schema_version": 1, "model_alias": alias,
                        "llm_release_ref": ref, "profile_id": intent["profile"]["profileId"],
                        "onboarding_id": intent["onboarding_id"]}
        attestation = {"schema_version": 1, "status": "READY", "scope": "recommendation",
                       "llm_release_ref": ref, "onboarding_id": intent["onboarding_id"],
                       "policy_checksum": intent["policy_checksum"],
                       "candidate_release_id": intent["candidate"]["release_id"]}
        evidence_with_uris = {
            **evidence,
            **{name + "_uri": "s3://" + BUCKET + "/" + key
               for name, key in keys.items()},
        }
        for key, value in ((keys["profile"], intent["profile"]),
                           (keys["catalog"], intent["catalog"]),
                           (keys["alias"], alias_record),
                           (keys["attestation"], attestation),
                           (keys["evidence"], evidence_with_uris)):
            _put_once(self.client, key, value)
        return {name + "_uri": "s3://" + BUCKET + "/" + key for name, key in keys.items()}

    def _retire_failed_candidate(self, onboarding_id, candidate, state):
        with self.db.connect() as connection:
            retained = connection.execute(
                """SELECT 1 FROM recsys_ab.model_onboardings
                   WHERE candidate_release_id=%s AND onboarding_id<>%s
                     AND status='READY' AND expires_at>now() LIMIT 1""",
                (candidate["release_id"], onboarding_id),
            ).fetchone()
            active_session = connection.execute(
                """SELECT 1 FROM recsys_ab.sessions
                   WHERE release_id=%s AND closed_at IS NULL
                     AND (expires_at IS NULL OR expires_at>now()) LIMIT 1""",
                (candidate["release_id"],),
            ).fetchone()
        releases = [state.get("champion"), state.get("previous")]
        if state.get("phase") not in {"IDLE", "COMPLETED", "ROLLED_BACK"}:
            releases.append(state.get("pending"))
        protected = {item["release_id"] for item in releases if item}
        if retained or active_session or candidate["release_id"] in protected:
            return
        self.driver.retire_release_capacity(
            [candidate["release_id"]],
            {item["llm_version_id"] for item in releases if item},
        )

    def prepare(self, intent_uri: str):
        policy, intent = load_policy(), self._load(intent_uri)
        candidate = validate_intent(intent, policy)
        self.db.migrate()
        row = self.repo.ensure(intent, intent_uri)
        if row["status"] == "READY":
            return _object(self.client, "recommendation/onboarding/" + intent["onboarding_id"] + "/evidence.json")
        if row["status"] in {"BLOCKED", "CLEANED"}:
            raise ValueError("onboarding is terminal; use a new reviewed alias or prepare an expired alias")
        deployed = False
        state = None
        try:
            state = self._state()
            champion = release(state["champion"])
            if state["phase"] not in {"IDLE", "COMPLETED", "ROLLED_BACK"}:
                raise ValueError("Recommendation experiment is active")
            if champion["release_id"] != intent["expected_champion"]:
                raise ValueError("champion changed after model discovery")
            if candidate["release_id"] in state.get("disabled", []):
                raise ValueError("candidate release is quarantined")
            validate_experiment(champion, candidate, "llm_only")
            fixtures = json.loads(Path("configs/llm-ab/cases.json").read_text())
            self.driver.preflight(champion, candidate, fixtures)
            route_before = self.driver.snapshot_route()
            self.repo.mark(intent["onboarding_id"], "ATTESTED", build_url=os.environ.get("BUILD_URL"))
            capacity = _deployment_capacity(self.driver, candidate)
            self.repo.mark(intent["onboarding_id"], "CAPACITY_OK")
            self.repo.mark(intent["onboarding_id"], "DOWNLOADING")
            startup_started = time.monotonic()
            self.driver.deploy(candidate)
            deployed = True
            startup_seconds = time.monotonic() - startup_started
            self.repo.mark(intent["onboarding_id"], "SERVING")
            self.driver.verify_release(candidate)
            if self.driver.snapshot_route() != route_before:
                raise ValueError("onboarding changed the production route")
            self.repo.mark(intent["onboarding_id"], "COMPATIBILITY")
            desired = compatibility_job(intent, self.driver.image, self.driver.namespace)
            job_name = desired["metadata"]["name"]
            raw = self.driver.kube("get", "job", job_name, "--ignore-not-found", "-o", "json")
            if not raw:
                self.driver.kube("create", "-f", "-", stdin=json.dumps(desired))
            peak = _wait_compatibility(
                self.driver, job_name, "rec-llm-" + candidate["llm_version_id"][:20]
            )
            results = self.repo.compatibility(intent["onboarding_id"])
            if (len(results) != 6 or {row["case_id"] for row in results} != set(CASES)
                    or any(row["status"] != "PASS" or not row["finished_at"] for row in results)):
                raise ValueError("candidate did not pass all six compatibility cases")
            memory_limit = quantity(candidate["llm"]["serving"]["resources"]["limits"]["memory"])
            if peak > memory_limit * quantity("0.8"):
                raise ValueError("peak model working set exceeds 80% of its limit")
            headroom = _node_memory_headroom(
                self.driver, "rec-llm-" + candidate["llm_version_id"][:20]
            )
            latencies = [float(row["latency_seconds"]) for row in results]
            now = self.clock()
            expires = now + policy["prepared_ttl_seconds"]
            evidence = {
                "schema_version": 1,
                "status": "READY",
                "onboarding_id": intent["onboarding_id"],
                "model_alias": intent["model_alias"],
                "llm_release_ref": intent["llm_release_ref"],
                "candidate_release_id": candidate["release_id"],
                "traffic_weight": 0,
                "backend_ready": True,
                "compatibility": "6/6",
                "startup_latency_seconds": {"p50": startup_seconds, "p95": startup_seconds},
                "latency_seconds": {"p50": statistics.median(latencies),
                                    "p95": _percentile(latencies, 0.95)},
                "peak_working_set_bytes": int(peak),
                "node_memory": headroom,
                "capacity_after_requests": capacity,
                "prepared_at": now,
                "expires_at": expires,
            }
            uris = self._publish(intent, evidence)
            # Read back every published object using the runtime/poller S3
            # credential before exposing a runnable Langfuse configuration.
            from .provision import secret
            from .llm_ab_start import _client
            poller = secret("kagent", "recsys-recommendation-trigger")
            if not poller:
                raise ValueError("Recommendation poller credential is unavailable")
            poller_client = _client(os.environ["MODEL_STORE_ENDPOINT"], poller)
            for uri in uris.values():
                bucket, key = parse_s3_uri(uri)
                poller_client.get_object(Bucket=bucket, Key=key)["Body"].read()
            self.repo.mark(intent["onboarding_id"], "READY", evidence_uri=uris["evidence_uri"],
                           prepared_at=datetime.fromtimestamp(now, timezone.utc),
                           expires_at=datetime.fromtimestamp(expires, timezone.utc))
            return {**evidence, **uris}
        except Exception as exc:
            reason = type(exc).__name__ + ": " + str(exc)[:300]
            if deployed and state is not None:
                try:
                    self._retire_failed_candidate(intent["onboarding_id"], candidate, state)
                except Exception as cleanup_error:
                    reason += "; cleanup=" + type(cleanup_error).__name__
            self.repo.mark(intent["onboarding_id"], "BLOCKED", reason=reason)
            raise

    def cleanup(self, intent_uri: str):
        intent = self._load(intent_uri)
        candidate = validate_intent(intent)
        self.db.migrate()
        self.repo.ensure(intent, intent_uri)
        state = self._state()
        protected = {item.get("release_id") for item in
                     (state.get("champion"), state.get("previous")) if item}
        if state.get("phase") not in {"IDLE", "COMPLETED", "ROLLED_BACK"} and state.get("pending"):
            protected.add(state["pending"]["release_id"])
        if candidate["release_id"] in protected and state.get("phase") not in {"COMPLETED", "ROLLED_BACK", "IDLE"}:
            raise ValueError("prepared candidate belongs to an active experiment")
        with self.db.connect() as connection:
            active = connection.execute(
                """SELECT 1 FROM recsys_ab.sessions
                   WHERE release_id=%s AND closed_at IS NULL
                     AND (expires_at IS NULL OR expires_at>now()) LIMIT 1""",
                (candidate["release_id"],),
            ).fetchone()
        if active or candidate["release_id"] in protected:
            raise ValueError("prepared candidate still has a protected consumer")
        result = self.driver.retire_release_capacity(
            [candidate["release_id"]],
            {item["llm_version_id"] for item in
             (state.get("champion"), state.get("previous")) if item}
            | ({state["pending"]["llm_version_id"]}
               if state.get("phase") not in {"IDLE", "COMPLETED", "ROLLED_BACK"}
               and state.get("pending") else set()),
        )
        self.repo.mark(intent["onboarding_id"], "CLEANED",
                       cleaned_at=datetime.fromtimestamp(self.clock(), timezone.utc))
        return {"status": "CLEANED", "onboarding_id": intent["onboarding_id"],
                "capacity": result, "catalog_retained": True, "inference_requests": 0}


def _load_env():
    if not os.environ.get("AB_ENV_FILE"):
        return
    values = json.loads(Path(os.environ["AB_ENV_FILE"]).read_text())
    allowed = {"AB_DATABASE_URL", "AB_INTERNAL_TOKEN", "AB_ROUTER_URL", "AB_NAMESPACE",
               "AB_SECRET_NAME", "MODEL_STORE_ENDPOINT", "AWS_ACCESS_KEY_ID",
               "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION", "AB_STATE_URI",
               "AB_RECOMMENDATION_STATE_URI", "AB_CASE_TICKET_KEY",
               "AB_LIVE_TEST_TOKEN"}
    if set(values) - allowed or not all(isinstance(value, str) for value in values.values()):
        raise ValueError("invalid onboarding credential file")
    os.environ.update(values)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "cleanup"])
    parser.add_argument("--intent-uri", required=True)
    args = parser.parse_args(argv)
    _load_env()
    service = Onboarding()
    result = service.prepare(args.intent_uri) if args.action == "prepare" else service.cleanup(args.intent_uri)
    output = Path(".llm-onboarding")
    output.mkdir(exist_ok=True)
    (output / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": result["status"], "onboarding_id": result["onboarding_id"]}))


if __name__ == "__main__":
    main()
