from __future__ import annotations

import json
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DDL = """
CREATE SCHEMA IF NOT EXISTS recsys_ab;
CREATE TABLE IF NOT EXISTS recsys_ab.sessions (
  session_key text PRIMARY KEY, release_id text NOT NULL,
  backend_context text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE recsys_ab.sessions ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE recsys_ab.sessions ADD COLUMN IF NOT EXISTS last_seen_at timestamptz;
ALTER TABLE recsys_ab.sessions ADD COLUMN IF NOT EXISTS expires_at timestamptz;
ALTER TABLE recsys_ab.sessions ADD COLUMN IF NOT EXISTS closed_at timestamptz;
ALTER TABLE recsys_ab.sessions ADD COLUMN IF NOT EXISTS close_reason text;
UPDATE recsys_ab.sessions SET last_seen_at=created_at WHERE last_seen_at IS NULL;
ALTER TABLE recsys_ab.sessions ALTER COLUMN last_seen_at SET DEFAULT now();
ALTER TABLE recsys_ab.sessions ALTER COLUMN last_seen_at SET NOT NULL;
ALTER TABLE recsys_ab.sessions DROP CONSTRAINT IF EXISTS sessions_source_check;
ALTER TABLE recsys_ab.sessions ADD CONSTRAINT sessions_source_check
  CHECK(source IS NULL OR source IN ('production','synthetic','live_test'));
CREATE INDEX IF NOT EXISTS ab_active_sessions_by_release
  ON recsys_ab.sessions(release_id) WHERE closed_at IS NULL;
CREATE TABLE IF NOT EXISTS recsys_ab.invocations (
  request_key text PRIMARY KEY, session_key text NOT NULL REFERENCES recsys_ab.sessions,
  experiment_id text NOT NULL, source text NOT NULL CHECK(source IN ('production','synthetic')),
  release_id text NOT NULL, started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz, result jsonb, response jsonb, task_id text
);
ALTER TABLE recsys_ab.invocations ADD COLUMN IF NOT EXISTS request_hash text;
CREATE INDEX IF NOT EXISTS ab_observation ON recsys_ab.invocations(experiment_id, source, started_at);
ALTER TABLE recsys_ab.invocations DROP CONSTRAINT IF EXISTS invocations_source_check;
ALTER TABLE recsys_ab.invocations ADD CONSTRAINT invocations_source_check CHECK(source IN ('production','synthetic','live_test'));
CREATE TABLE IF NOT EXISTS recsys_ab.heartbeat (
  instance text PRIMARY KEY, seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS recsys_ab.evaluation_outbox (
  request_key text PRIMARY KEY, experiment_id text NOT NULL,
  snapshot jsonb NOT NULL, metadata jsonb NOT NULL,
  evaluation jsonb, confirmed_at timestamptz,
  attempts integer NOT NULL DEFAULT 0, next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error text, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ab_evaluation_pending ON recsys_ab.evaluation_outbox(next_attempt_at)
  WHERE confirmed_at IS NULL;
CREATE TABLE IF NOT EXISTS recsys_ab.execution_claims (
  release_id text NOT NULL, session_id text NOT NULL, turn_id text NOT NULL,
  role text NOT NULL, tool_name text NOT NULL, call_id text NOT NULL,
  args jsonb NOT NULL, result jsonb, status text NOT NULL CHECK(status IN ('CLAIMED','COMPLETED','FAILED')),
  claimed_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
  PRIMARY KEY(release_id,session_id,turn_id,role,tool_name)
);
CREATE TABLE IF NOT EXISTS recsys_ab.execution_violations (
 release_id text NOT NULL, session_id text NOT NULL, turn_id text NOT NULL,
 role text NOT NULL, reason text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS execution_violations_turn ON recsys_ab.execution_violations(release_id,session_id,turn_id,role);
CREATE TABLE IF NOT EXISTS recsys_ab.compatibility_requests (
 request_key text PRIMARY KEY, experiment_id text NOT NULL, variant text NOT NULL,
 case_id text NOT NULL, release_id text NOT NULL, fixture_checksum text NOT NULL,
 started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz, result jsonb,
 UNIQUE(experiment_id,variant,case_id)
);
CREATE TABLE IF NOT EXISTS recsys_ab.live_load_runs (
 experiment_id text PRIMARY KEY, claimed_at timestamptz NOT NULL DEFAULT now(),
 submitted integer NOT NULL DEFAULT 0 CHECK(submitted BETWEEN 0 AND 360)
);
CREATE TABLE IF NOT EXISTS recsys_ab.controller_actions (
 action_key text PRIMARY KEY, experiment_id text NOT NULL,
 action text NOT NULL CHECK(action IN ('prepare','route','promote','rollback','cleanup')),
 target_weight integer CHECK(target_weight IN (0,10,50,100)),
 expected_phase text NOT NULL, expected_state_etag text NOT NULL,
 status text NOT NULL CHECK(status IN
   ('INTENT','SUBMITTED','RUNNING','SUCCEEDED','FAILED','NEEDS_ATTENTION')),
 queue_id text, build_number integer, build_url text, reason text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ab_controller_actions_experiment
 ON recsys_ab.controller_actions(experiment_id,created_at);
CREATE TABLE IF NOT EXISTS recsys_ab.controller_status (
 experiment_id text PRIMARY KEY, decision text NOT NULL, reason text,
 last_tick_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS recsys_ab.traffic_runs (
 experiment_id text PRIMARY KEY, job_name text NOT NULL UNIQUE,
 manifest_checksum text NOT NULL, status text NOT NULL CHECK(status IN
   ('CREATED','RUNNING','COMPLETED','FAILED','LOST','NEEDS_ATTENTION')),
 live_submitted integer NOT NULL DEFAULT 0 CHECK(live_submitted BETWEEN 0 AND 360),
 synthetic_submitted integer NOT NULL DEFAULT 0 CHECK(synthetic_submitted BETWEEN 0 AND 20),
 phase text, reason text, created_at timestamptz NOT NULL DEFAULT now(),
 started_at timestamptz, heartbeat_at timestamptz, finished_at timestamptz,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS recsys_ab.live_traffic_tickets (
 request_id text PRIMARY KEY, experiment_id text NOT NULL REFERENCES recsys_ab.traffic_runs,
 sequence integer NOT NULL, phase text NOT NULL CHECK(phase IN ('CANARY','AB','VERIFY')),
 nonce text NOT NULL UNIQUE, prompt_sha256 text NOT NULL, expires_at timestamptz NOT NULL,
 status text NOT NULL CHECK(status IN
   ('DISPATCH_INTENT','CLAIMED','COMPLETED','AMBIGUOUS','REJECTED')),
 issued_at timestamptz NOT NULL DEFAULT now(), claimed_at timestamptz,
 finished_at timestamptz, http_status integer, response_checksum text,
 latency_seconds double precision CHECK(latency_seconds >= 0), edge_revision text,
 last_error text, UNIQUE(experiment_id,sequence)
);
CREATE INDEX IF NOT EXISTS ab_live_traffic_ticket_status
 ON recsys_ab.live_traffic_tickets(experiment_id,status);
CREATE TABLE IF NOT EXISTS recsys_ab.external_case_runs (
 experiment_id text PRIMARY KEY, fixture_checksum text NOT NULL,
 status text NOT NULL CHECK(status IN ('DISPATCHING','COMPLETED','HOLD')),
 started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz
);
CREATE TABLE IF NOT EXISTS recsys_ab.external_case_tickets (
 experiment_id text NOT NULL REFERENCES recsys_ab.external_case_runs(experiment_id),
 case_id text NOT NULL, request_id text NOT NULL UNIQUE, nonce text NOT NULL UNIQUE,
 fixture_checksum text NOT NULL, prompt_sha256 text NOT NULL, expires_at timestamptz NOT NULL,
 status text NOT NULL CHECK(status IN ('ISSUED','DISPATCH_INTENT','CLAIMED','COMPLETED','AMBIGUOUS','REJECTED')),
 issued_at timestamptz NOT NULL DEFAULT now(), intent_at timestamptz, claimed_at timestamptz,
 finished_at timestamptz, http_status integer, response_checksum text,
 latency_seconds double precision CHECK(latency_seconds >= 0), edge_revision text,
 last_error text,
 PRIMARY KEY(experiment_id,case_id)
);
CREATE INDEX IF NOT EXISTS ab_external_ticket_status
 ON recsys_ab.external_case_tickets(experiment_id,status);
ALTER TABLE recsys_ab.external_case_tickets
 ADD COLUMN IF NOT EXISTS latency_seconds double precision;
ALTER TABLE recsys_ab.external_case_tickets
 ADD COLUMN IF NOT EXISTS edge_revision text;
CREATE TABLE IF NOT EXISTS recsys_ab.model_onboardings (
 onboarding_id text PRIMARY KEY, scope text NOT NULL, model_alias text NOT NULL,
 artifact_identity jsonb NOT NULL, policy_checksum text NOT NULL,
 llm_release_ref text NOT NULL, candidate_release_id text NOT NULL,
 intent_uri text NOT NULL, status text NOT NULL,
 reason text, build_url text, evidence_uri text,
 prepared_at timestamptz, expires_at timestamptz,
 cleanup_requested_at timestamptz, cleaned_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(scope,model_alias)
);
CREATE INDEX IF NOT EXISTS ab_ready_onboardings_expiry
 ON recsys_ab.model_onboardings(expires_at)
 WHERE status='READY';
CREATE TABLE IF NOT EXISTS recsys_ab.onboarding_compatibility (
 onboarding_id text NOT NULL REFERENCES recsys_ab.model_onboardings,
 case_id text NOT NULL, status text NOT NULL,
 result jsonb, latency_seconds double precision,
 created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 PRIMARY KEY(onboarding_id,case_id)
);
"""


class Database:
    def __init__(self, dsn):
        self.dsn = dsn

    def connect(self):
        return psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)

    def claim_live_load(self, experiment_id):
        # A replacement Pod/Job cannot obtain a fresh 360-request budget.
        # An ambiguous previous owner is deliberately not resumed or replayed.
        with self.connect() as c:
            return c.execute("""INSERT INTO recsys_ab.live_load_runs(experiment_id)
                VALUES (%s) ON CONFLICT DO NOTHING RETURNING experiment_id""",
                (experiment_id,)).fetchone() is not None

    def claim_live_request(self, experiment_id):
        with self.connect() as c:
            row = c.execute("""UPDATE recsys_ab.live_load_runs SET submitted=submitted+1
                WHERE experiment_id=%s AND submitted<360 RETURNING submitted""",
                (experiment_id,)).fetchone()
            return row['submitted'] if row else None

    def create_controller_action(self, row):
        """Persist one immutable controller decision before Jenkins delivery."""
        required = {
            "action_key", "experiment_id", "action", "target_weight",
            "expected_phase", "expected_state_etag",
        }
        if set(row) != required:
            raise ValueError("invalid controller action")
        with self.connect() as c:
            created = c.execute(
                """INSERT INTO recsys_ab.controller_actions
                   (action_key,experiment_id,action,target_weight,expected_phase,
                    expected_state_etag,status)
                   VALUES (%s,%s,%s,%s,%s,%s,'INTENT')
                   ON CONFLICT DO NOTHING RETURNING *""",
                tuple(row[key] for key in (
                    "action_key", "experiment_id", "action", "target_weight",
                    "expected_phase", "expected_state_etag",
                )),
            ).fetchone()
            if created:
                return dict(created), True
            existing = c.execute(
                "SELECT * FROM recsys_ab.controller_actions WHERE action_key=%s",
                (row["action_key"],),
            ).fetchone()
            if not existing or any(
                existing[key] != row[key] for key in required
            ):
                raise ValueError("controller action identity conflict")
            return dict(existing), False

    def controller_action(self, action_key):
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM recsys_ab.controller_actions WHERE action_key=%s",
                (action_key,),
            ).fetchone()
        return dict(row) if row else None

    def latest_controller_action(self, experiment_id):
        with self.connect() as c:
            row = c.execute(
                """SELECT * FROM recsys_ab.controller_actions
                   WHERE experiment_id=%s ORDER BY created_at DESC LIMIT 1""",
                (experiment_id,),
            ).fetchone()
        return dict(row) if row else None

    def record_controller_tick(self, experiment_id, decision, reason=None):
        with self.connect() as c:
            c.execute(
                """INSERT INTO recsys_ab.controller_status
                   (experiment_id,decision,reason,last_tick_at) VALUES (%s,%s,%s,now())
                   ON CONFLICT(experiment_id) DO UPDATE SET decision=excluded.decision,
                   reason=excluded.reason,last_tick_at=excluded.last_tick_at""",
                (experiment_id, str(decision)[:64], (reason or "")[:160] or None),
            )

    def update_controller_action(self, action_key, status, **values):
        allowed = {"queue_id", "build_number", "build_url", "reason"}
        if set(values) - allowed:
            raise ValueError("invalid controller action update")
        fields = ["status=%s", "updated_at=now()"] + [key + "=%s" for key in values]
        with self.connect() as c:
            row = c.execute(
                "UPDATE recsys_ab.controller_actions SET " + ",".join(fields)
                + " WHERE action_key=%s RETURNING *",
                (status, *values.values(), action_key),
            ).fetchone()
        if not row:
            raise ValueError("unknown controller action")
        return dict(row)

    def begin_traffic_run(self, experiment_id, job_name, manifest_checksum):
        """Claim the one lifetime Traffic Job for an experiment."""
        with self.connect() as c:
            created = c.execute(
                """INSERT INTO recsys_ab.traffic_runs
                   (experiment_id,job_name,manifest_checksum,status)
                   VALUES (%s,%s,%s,'CREATED') ON CONFLICT DO NOTHING RETURNING *""",
                (experiment_id, job_name, manifest_checksum),
            ).fetchone()
            if created:
                return dict(created), True
            existing = c.execute(
                "SELECT * FROM recsys_ab.traffic_runs WHERE experiment_id=%s",
                (experiment_id,),
            ).fetchone()
            if (
                not existing
                or existing["job_name"] != job_name
                or existing["manifest_checksum"] != manifest_checksum
            ):
                raise ValueError("traffic run immutable identity conflict")
            return dict(existing), False

    def traffic_run(self, experiment_id):
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM recsys_ab.traffic_runs WHERE experiment_id=%s",
                (experiment_id,),
            ).fetchone()
        return dict(row) if row else None

    def trigger_status(self, experiment_id):
        """Return controller-owned trigger lifecycle state for a Traffic Job."""
        with self.connect() as c:
            row = c.execute(
                "SELECT status FROM recsys_ab.trigger_requests WHERE experiment_id=%s",
                (experiment_id,),
            ).fetchone()
        return row["status"] if row else None

    def heartbeat_traffic_run(self, experiment_id, phase):
        with self.connect() as c:
            row = c.execute(
                """UPDATE recsys_ab.traffic_runs SET status='RUNNING',phase=%s,
                   started_at=coalesce(started_at,now()),heartbeat_at=now(),updated_at=now()
                   WHERE experiment_id=%s AND status IN ('CREATED','RUNNING')
                   RETURNING *""",
                (phase, experiment_id),
            ).fetchone()
        if not row:
            raise ValueError("traffic run is not runnable")
        return dict(row)

    def finish_traffic_run(self, experiment_id, status, reason=None):
        if status not in {"COMPLETED", "FAILED", "LOST", "NEEDS_ATTENTION"}:
            raise ValueError("invalid traffic run terminal status")
        with self.connect() as c:
            c.execute(
                """UPDATE recsys_ab.traffic_runs SET status=%s,reason=%s,
                   finished_at=now(),updated_at=now() WHERE experiment_id=%s""",
                (status, (reason or "")[:160] or None, experiment_id),
            )

    def issue_live_ticket(self, claims):
        """Persist a live request intent and consume its bounded budget once."""
        with self.connect() as c, c.transaction():
            run = c.execute(
                "SELECT * FROM recsys_ab.traffic_runs WHERE experiment_id=%s FOR UPDATE",
                (claims["experiment_id"],),
            ).fetchone()
            if not run or run["status"] != "RUNNING" or run["live_submitted"] >= 360:
                return None
            sequence = run["live_submitted"] + 1
            c.execute(
                """INSERT INTO recsys_ab.live_traffic_tickets
                   (request_id,experiment_id,sequence,phase,nonce,prompt_sha256,
                    expires_at,status) VALUES (%s,%s,%s,%s,%s,%s,to_timestamp(%s),
                    'DISPATCH_INTENT')""",
                (
                    claims["request_id"], claims["experiment_id"], sequence,
                    claims["phase"], claims["nonce"], claims["prompt_sha256"],
                    claims["expires_at"],
                ),
            )
            c.execute(
                """UPDATE recsys_ab.traffic_runs SET live_submitted=%s,
                   updated_at=now() WHERE experiment_id=%s""",
                (sequence, claims["experiment_id"]),
            )
            return sequence

    def claim_live_ticket(self, claims):
        with self.connect() as c, c.transaction():
            row = c.execute(
                """SELECT * FROM recsys_ab.live_traffic_tickets
                   WHERE request_id=%s FOR UPDATE""",
                (claims["request_id"],),
            ).fetchone()
            if not row or any(
                str(row[key]) != str(claims[key])
                for key in ("experiment_id", "phase", "nonce", "prompt_sha256")
            ) or int(row["expires_at"].timestamp()) != claims["expires_at"]:
                raise ValueError("live ticket is not registered")
            if row["status"] != "DISPATCH_INTENT":
                raise ValueError("live ticket already claimed; do not replay")
            c.execute(
                """UPDATE recsys_ab.live_traffic_tickets SET status='CLAIMED',
                   claimed_at=now() WHERE request_id=%s""",
                (claims["request_id"],),
            )

    def finish_live_ticket(self, request_id, status, *, http_status=None,
                           response_checksum=None, latency_seconds=None,
                           edge_revision=None, last_error=None):
        if status not in {"COMPLETED", "AMBIGUOUS", "REJECTED"}:
            raise ValueError("invalid live ticket state")
        with self.connect() as c:
            row = c.execute(
                """UPDATE recsys_ab.live_traffic_tickets SET status=%s,
                   finished_at=now(),http_status=%s,response_checksum=%s,
                   latency_seconds=%s,edge_revision=%s,last_error=%s
                   WHERE request_id=%s AND status IN ('DISPATCH_INTENT','CLAIMED')
                   RETURNING request_id""",
                (
                    status, http_status, response_checksum, latency_seconds,
                    edge_revision, (last_error or "")[:120] or None, request_id,
                ),
            ).fetchone()
        if not row:
            raise ValueError("live ticket is not pending")

    def mark_live_ambiguous(self, request_id, last_error):
        """Seal a client-side unknown result without overwriting edge evidence."""
        with self.connect() as c:
            row = c.execute(
                """UPDATE recsys_ab.live_traffic_tickets SET status='AMBIGUOUS',
                   finished_at=now(),last_error=%s
                   WHERE request_id=%s AND status IN ('DISPATCH_INTENT','CLAIMED')
                   RETURNING status""",
                ((last_error or "transport")[:120], request_id),
            ).fetchone()
            if row:
                return row["status"]
            existing = c.execute(
                "SELECT status FROM recsys_ab.live_traffic_tickets WHERE request_id=%s",
                (request_id,),
            ).fetchone()
        return existing["status"] if existing else None

    def traffic_evidence(self, experiment_id):
        with self.connect() as c:
            run = c.execute(
                "SELECT * FROM recsys_ab.traffic_runs WHERE experiment_id=%s",
                (experiment_id,),
            ).fetchone()
            rows = c.execute(
                """SELECT status,count(*) AS count FROM recsys_ab.live_traffic_tickets
                   WHERE experiment_id=%s GROUP BY status""",
                (experiment_id,),
            ).fetchall()
        return {
            "run": dict(run) if run else None,
            "live_tickets": {row["status"]: row["count"] for row in rows},
            "external": self.external_suite(experiment_id),
        }

    def begin_external_suite(self, experiment_id, fixture_checksum, tickets):
        """Create the immutable 20-ticket suite once; an existing suite is never resent."""
        if len(tickets) != 20 or len({row["case_id"] for row in tickets}) != 20:
            raise ValueError("external suite requires exactly 20 unique cases")
        with self.connect() as c, c.transaction():
            created = c.execute(
                """INSERT INTO recsys_ab.external_case_runs
                   (experiment_id,fixture_checksum,status) VALUES (%s,%s,'DISPATCHING')
                   ON CONFLICT DO NOTHING RETURNING experiment_id""",
                (experiment_id, fixture_checksum),
            ).fetchone()
            if created:
                for row in tickets:
                    c.execute(
                        """INSERT INTO recsys_ab.external_case_tickets
                           (experiment_id,case_id,request_id,nonce,fixture_checksum,
                            prompt_sha256,expires_at,status)
                           VALUES (%s,%s,%s,%s,%s,%s,to_timestamp(%s),'ISSUED')""",
                        (experiment_id, row["case_id"], row["request_id"], row["nonce"],
                         fixture_checksum, row["prompt_sha256"], row["expires_at"]),
                    )
                c.execute(
                    """UPDATE recsys_ab.traffic_runs SET synthetic_submitted=20,
                       updated_at=now() WHERE experiment_id=%s""",
                    (experiment_id,),
                )
                return True
            run = c.execute(
                "SELECT fixture_checksum FROM recsys_ab.external_case_runs WHERE experiment_id=%s",
                (experiment_id,),
            ).fetchone()
            existing = c.execute(
                """SELECT case_id,request_id,nonce,fixture_checksum,prompt_sha256,
                          extract(epoch FROM expires_at)::bigint AS expires_at
                   FROM recsys_ab.external_case_tickets WHERE experiment_id=%s
                   ORDER BY case_id""",
                (experiment_id,),
            ).fetchall()
            expected = sorted(
                [{key: row[key] for key in (
                    "case_id", "request_id", "nonce", "prompt_sha256", "expires_at"
                 )} | {"fixture_checksum": fixture_checksum} for row in tickets],
                key=lambda row: row["case_id"],
            )
            if not run or run["fixture_checksum"] != fixture_checksum or [dict(r) for r in existing] != expected:
                raise ValueError("external suite immutable identity conflict")
            return False

    def mark_ticket_intent(self, experiment_id, case_id):
        with self.connect() as c:
            row = c.execute(
                """UPDATE recsys_ab.external_case_tickets
                   SET status='DISPATCH_INTENT',intent_at=now()
                   WHERE experiment_id=%s AND case_id=%s AND status='ISSUED'
                   RETURNING request_id""",
                (experiment_id, case_id),
            ).fetchone()
            if not row:
                raise ValueError("ticket is not issuable; do not replay")
            return row["request_id"]

    def claim_external_ticket(self, claims):
        with self.connect() as c, c.transaction():
            row = c.execute(
                """SELECT * FROM recsys_ab.external_case_tickets
                   WHERE experiment_id=%s AND case_id=%s FOR UPDATE""",
                (claims["experiment_id"], claims["case_id"]),
            ).fetchone()
            if not row or any(
                str(row[key]) != str(claims[key])
                for key in ("request_id", "nonce", "fixture_checksum", "prompt_sha256")
            ) or int(row["expires_at"].timestamp()) != claims["expires_at"]:
                raise ValueError("ticket is not registered")
            if row["status"] != "DISPATCH_INTENT":
                raise ValueError("ticket already claimed; do not replay")
            c.execute(
                """UPDATE recsys_ab.external_case_tickets
                   SET status='CLAIMED',claimed_at=now()
                   WHERE experiment_id=%s AND case_id=%s""",
                (claims["experiment_id"], claims["case_id"]),
            )

    def finish_external_ticket(self, experiment_id, case_id, status, *,
                               http_status=None, response_checksum=None, latency_seconds=None,
                               edge_revision=None, last_error=None):
        if status not in {"COMPLETED", "AMBIGUOUS", "REJECTED"}:
            raise ValueError("invalid terminal ticket state")
        safe_error = (last_error or "")[:120] or None
        with self.connect() as c, c.transaction():
            row = c.execute(
                """UPDATE recsys_ab.external_case_tickets SET status=%s,
                   finished_at=now(),http_status=%s,response_checksum=%s,
                   latency_seconds=%s,edge_revision=%s,last_error=%s
                   WHERE experiment_id=%s AND case_id=%s AND status='CLAIMED'
                   RETURNING experiment_id""",
                (status, http_status, response_checksum, latency_seconds,
                 edge_revision, safe_error, experiment_id, case_id),
            ).fetchone()
            if not row:
                raise ValueError("ticket is not claimed")
            counts = c.execute(
                """SELECT count(*) FILTER(WHERE status='COMPLETED') AS completed,
                          count(*) FILTER(WHERE status IN ('AMBIGUOUS','REJECTED')) AS failed,
                          count(*) AS total
                   FROM recsys_ab.external_case_tickets WHERE experiment_id=%s""",
                (experiment_id,),
            ).fetchone()
            if counts["completed"] + counts["failed"] == counts["total"] == 20:
                c.execute(
                    """UPDATE recsys_ab.external_case_runs
                       SET status=%s,finished_at=now() WHERE experiment_id=%s""",
                    ("COMPLETED" if counts["completed"] == 20 else "HOLD", experiment_id),
                )

    def mark_external_ambiguous(self, experiment_id, case_id, last_error):
        """Record an uncertain public transport result without permitting replay."""
        with self.connect() as c, c.transaction():
            row = c.execute(
                """UPDATE recsys_ab.external_case_tickets
                   SET status='AMBIGUOUS',finished_at=now(),last_error=%s
                   WHERE experiment_id=%s AND case_id=%s
                     AND status IN ('DISPATCH_INTENT','CLAIMED')
                   RETURNING experiment_id""",
                ((last_error or "transport")[:120], experiment_id, case_id),
            ).fetchone()
            if not row:
                return
            counts = c.execute(
                """SELECT count(*) FILTER(WHERE status IN ('COMPLETED','AMBIGUOUS','REJECTED')) AS terminal,
                          count(*) FILTER(WHERE status='COMPLETED') AS completed,
                          count(*) AS total
                   FROM recsys_ab.external_case_tickets WHERE experiment_id=%s""",
                (experiment_id,),
            ).fetchone()
            if counts["terminal"] == counts["total"] == 20:
                c.execute(
                    """UPDATE recsys_ab.external_case_runs SET status=%s,finished_at=now()
                       WHERE experiment_id=%s""",
                    ("COMPLETED" if counts["completed"] == 20 else "HOLD", experiment_id),
                )

    def external_suite(self, experiment_id):
        with self.connect() as c:
            run = c.execute(
                "SELECT * FROM recsys_ab.external_case_runs WHERE experiment_id=%s",
                (experiment_id,),
            ).fetchone()
            rows = c.execute(
                """SELECT status,count(*) AS count FROM recsys_ab.external_case_tickets
                   WHERE experiment_id=%s GROUP BY status""",
                (experiment_id,),
            ).fetchall()
        return {"run": dict(run) if run else None,
                "tickets": {row["status"]: row["count"] for row in rows}}

    def source_inflight(self, experiment_id, source):
        with self.connect() as c:
            return c.execute(
                """SELECT count(*) AS n FROM recsys_ab.invocations
                   WHERE experiment_id=%s AND source=%s AND finished_at IS NULL""",
                (experiment_id, source),
            ).fetchone()["n"]

    def retire_terminal_sessions(self, disabled_release_ids=()):
        """Close disposable sessions and return releases that still need serving.

        Old rows did not record their source.  Backfill them from durable
        invocation evidence, preferring production if a legacy session was
        ever used by more than one source.  Unknown and unfinished sessions
        remain protected; cleanup never guesses that they are disposable.
        """
        disabled = sorted(set(disabled_release_ids))
        with self.connect() as c, c.transaction():
            c.execute(
                """WITH inferred AS (
                     SELECT s.session_key,
                       CASE
                         WHEN bool_or(i.source='production') THEN 'production'
                         WHEN bool_or(i.source='live_test') THEN 'live_test'
                         WHEN bool_or(i.source='synthetic') THEN 'synthetic'
                       END AS source
                     FROM recsys_ab.sessions s
                     LEFT JOIN recsys_ab.invocations i USING(session_key)
                     WHERE s.source IS NULL
                     GROUP BY s.session_key
                   )
                   UPDATE recsys_ab.sessions s SET source=inferred.source
                   FROM inferred
                   WHERE s.session_key=inferred.session_key
                     AND s.source IS NULL AND inferred.source IS NOT NULL"""
            )
            closed = c.execute(
                """UPDATE recsys_ab.sessions s
                   SET closed_at=now(),
                       close_reason=CASE WHEN release_id=ANY(%s::text[])
                         THEN 'release_quarantined' ELSE 'terminal_test_session' END
                   WHERE closed_at IS NULL
                     AND NOT EXISTS (
                       SELECT 1 FROM recsys_ab.invocations i
                       WHERE i.session_key=s.session_key AND i.finished_at IS NULL
                     )
                     AND (source IN ('synthetic','live_test') OR release_id=ANY(%s::text[]))
                   RETURNING release_id,source,close_reason""",
                (disabled, disabled),
            ).fetchall()
            active = c.execute(
                """SELECT release_id,source,count(*) AS sessions
                   FROM recsys_ab.sessions
                   WHERE closed_at IS NULL
                     AND (expires_at IS NULL OR expires_at>now())
                   GROUP BY release_id,source"""
            ).fetchall()
            unfinished = c.execute(
                """SELECT release_id,count(*) AS invocations
                   FROM recsys_ab.invocations WHERE finished_at IS NULL
                   GROUP BY release_id"""
            ).fetchall()
            closed_total = c.execute(
                "SELECT count(*) AS sessions FROM recsys_ab.sessions WHERE closed_at IS NOT NULL"
            ).fetchone()["sessions"]
        protected = {row["release_id"] for row in active}
        protected.update(row["release_id"] for row in unfinished)
        by_reason = {}
        for row in closed:
            reason = row["close_reason"]
            by_reason[reason] = by_reason.get(reason, 0) + 1
        return {
            "closed": len(closed),
            "closed_total": closed_total,
            "closed_by_reason": by_reason,
            "active_sessions": sum(row["sessions"] for row in active),
            "unfinished_invocations": sum(row["invocations"] for row in unfinished),
            "protected_release_ids": sorted(protected),
        }

    def synthetic_suite_complete(self, experiment_id, expected):
        with self.connect() as c:
            row = c.execute(
                """SELECT count(*) AS total,
                          count(*) FILTER (WHERE finished_at IS NOT NULL) AS completed
                   FROM recsys_ab.invocations
                   WHERE experiment_id=%s AND source='synthetic'""",
                (experiment_id,),
            ).fetchone()
        return row["total"] == expected and row["completed"] == expected

    def migrate(self):
        with self.connect() as c:
            # IF NOT EXISTS alone does not serialize concurrent catalog inserts.
            c.execute(
                "SELECT pg_advisory_lock(hashtextextended('recsys-ab-schema', 0))"
            )
            try:
                c.execute(DDL)
            finally:
                c.execute(
                    "SELECT pg_advisory_unlock(hashtextextended('recsys-ab-schema', 0))"
                )

    @contextmanager
    def session(self, key):
        with self.connect() as c:
            # Session-level lock survives autocommit; claim is committed BEFORE HTTP.
            c.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
            try:
                yield c
            finally:
                c.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))

    def result(self, key):
        with self.connect() as c:
            row = c.execute(
                "SELECT result FROM recsys_ab.invocations WHERE request_key=%s", (key,)
            ).fetchone()
            if not row or row["result"] is None:
                return None
            result = dict(row["result"])
            ev = c.execute("SELECT evaluation, confirmed_at FROM recsys_ab.evaluation_outbox WHERE request_key=%s", (key,)).fetchone()
            if ev:
                result["evaluation"] = {"verdict": (ev["evaluation"] or {}).get("verdict", "HOLD"),
                    "evaluator_version": (ev["evaluation"] or {}).get("evaluator_version"),
                    "synced": ev["confirmed_at"] is not None}
                if ev['evaluation']:
                    result['evaluation_evidence'] = ev['evaluation']
            return result

    def complete(self, key, evidence, response, task_id, evaluation=None):
        with self.connect() as c:
            with c.transaction():
                c.execute(
                    "UPDATE recsys_ab.invocations SET finished_at=now(), result=%s, response=%s, task_id=%s WHERE request_key=%s",
                    (Jsonb(evidence), Jsonb(response), task_id, key),
                )
                if evaluation is not None:
                    c.execute("""INSERT INTO recsys_ab.evaluation_outbox(request_key,experiment_id,snapshot,metadata)
                        SELECT request_key,experiment_id,%s,%s FROM recsys_ab.invocations
                        WHERE request_key=%s AND source='synthetic' ON CONFLICT(request_key) DO NOTHING""",
                        (Jsonb(evaluation["snapshot"]), Jsonb(evaluation["metadata"]), key))
                    saved = c.execute("SELECT snapshot,metadata FROM recsys_ab.evaluation_outbox WHERE request_key=%s", (key,)).fetchone()
                    if not saved or saved["snapshot"] != evaluation["snapshot"] or saved["metadata"] != evaluation["metadata"]:
                        raise ValueError("immutable evaluation snapshot conflict")

    def observe(self, state, start, end, source=None):
        source = source or state["policy"].get("sample_source", "production")
        with self.connect() as c:
            heartbeat = c.execute(
                "SELECT extract(epoch FROM max(seen_at)) AS at FROM recsys_ab.heartbeat WHERE instance LIKE %s",
                ("recsys-workflow-router%" if os.environ.get("AB_SCOPE") == "workflow" else "recsys-ab-router%",)
            ).fetchone()["at"]
            rows = c.execute(
                """
              SELECT release_id, count(*) FILTER(WHERE finished_at IS NOT NULL) AS count,
                count(*) FILTER(WHERE (result->>'error')::boolean) AS errors,
                count(*) FILTER(WHERE (result->>'contract_failure')::boolean) AS contract_failures,
                count(*) FILTER(WHERE result IS NULL OR result->>'verdict'='HOLD') AS unknown,
                percentile_cont(0.95) WITHIN GROUP(ORDER BY extract(epoch FROM finished_at-started_at)) AS p95
              FROM recsys_ab.invocations
              WHERE experiment_id=%s AND source=%s
                AND ((finished_at >= to_timestamp(%s) AND finished_at < to_timestamp(%s)
                      AND (%s OR started_at >= to_timestamp(%s)))
                     OR (finished_at IS NULL AND started_at < to_timestamp(%s)))
              GROUP BY release_id
            """,
                (
                    state["experiment_id"],
                    source,
                    start,
                    end,
                    state["phase"] == "MONITOR",
                    start,
                    end - state["policy"]["request_timeout_seconds"],
                ),
            ).fetchall()
        empty = {
            "count": 0,
            "errors": 0,
            "contract_failures": 0,
            "unknown": 0,
            "p95": None,
        }
        return {
            "healthy": heartbeat is not None
            and end - float(heartbeat) <= state["policy"]["telemetry_max_age_seconds"],
            **{
                arm: next(
                    (
                        dict(r)
                        for r in rows
                        if r["release_id"] == state[key]["release_id"]
                    ),
                    dict(empty),
                )
                for arm, key in (("champion", "baseline"), ("candidate", "pending"))
            },
        }

    def metrics(self):
        with self.connect() as c:
            return c.execute("""SELECT experiment_id, source, release_id,
              coalesce(result->>'verdict','STARTED') AS verdict, count(*) AS count,
              count(*) FILTER(WHERE (result->>'error')::boolean) AS errors,
              count(*) FILTER(WHERE (result->>'contract_failure')::boolean) AS contract_failures,
              count(DISTINCT session_key) AS sessions,
              sum((result->>'input_tokens')::bigint) AS input_tokens,
              sum((result->>'output_tokens')::bigint) AS output_tokens,
              percentile_cont(0.95) WITHIN GROUP(ORDER BY extract(epoch FROM finished_at-started_at)) AS p95
              FROM recsys_ab.invocations GROUP BY 1,2,3,4""").fetchall()


def json_label(value):
    return json.dumps(str(value), ensure_ascii=True)
