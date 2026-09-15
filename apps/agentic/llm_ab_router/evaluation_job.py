"""Finite, single-owner code evaluation + idempotent Langfuse delivery.

Contains no inference/agent/tool invocation path. Missing scores remain pending.
"""
import os
import time
import httpx
from psycopg.types.json import Jsonb
from .database import Database
from jenkins.python.llm_agent_cd.code_evaluation import evaluate, score_payloads


class ScoreSync:
    def __init__(self, client): self.client = client

    def confirm(self, payload):
        # Read before write. Langfuse ingestion is asynchronous and replacing
        # the same deterministic score immediately before every read can make
        # it transiently invisible forever. A later Job confirms the already
        # submitted score without re-posting it.
        try:
            self.readback(payload)
            return
        except ValueError as exc:
            if str(exc) != 'score_not_visible':
                raise
        self.client.post('/api/public/scores', json=payload).raise_for_status()
        raise ValueError('score_not_visible')

    def readback(self, payload):
        # Langfuse 4 uses v3 score reads. Ingestion may be asynchronous: an
        # empty result is pending, not success. The next Job uses the same ID.
        response = self.client.get('/api/public/v3/scores', params={"id": payload["id"], "fields": "details,subject"})
        response.raise_for_status()
        rows = response.json().get('data', [])
        matches = [r for r in rows if r.get('id') == payload['id']]
        if len(matches) != 1:
            raise ValueError('score_not_visible')
        row = matches[0]
        subject = ({'kind':'observation','id':payload['observationId'],'traceId':payload['traceId']}
                   if payload.get('observationId') else {'kind':'trace','id':payload['traceId']})
        if (row.get('name') != payload['name'] or row.get('dataType') != payload['dataType']
                or type(row.get('value')) is not type(payload['value']) and payload['dataType'] not in {'NUMERIC','BOOLEAN'}
                or row.get('value') != payload['value']
                or row.get('subject') != subject
                or any(row.get('metadata', {}).get(k) != v for k, v in payload['metadata'].items())):
            raise ValueError('score_roundtrip_mismatch')


def drain(db, sync, *, seconds=240):
    deadline = time.monotonic() + seconds
    processed = 0
    with db.connect() as c:
        if not c.execute("SELECT pg_try_advisory_lock(hashtextextended('recsys-recommendation-evaluation',0)) AS owned").fetchone()['owned']:
            return 0
        try:
            while time.monotonic() < deadline:
                row = c.execute("""SELECT * FROM recsys_ab.evaluation_outbox
                    WHERE confirmed_at IS NULL AND next_attempt_at<=now() ORDER BY created_at LIMIT 1""").fetchone()
                if not row:
                    break
                try:
                    kind = row['snapshot'].get('kind', 'workflow')
                    offline = kind == 'compatibility'
                    if offline:
                        from jenkins.python.llm_agent_cd.offline import evaluate as offline_evaluate
                        result = row['evaluation'] or offline_evaluate(row['snapshot'])
                    elif kind == 'recommendation':
                        from jenkins.python.llm_agent_cd.recommendation_evaluation import evaluate as recommendation_evaluate
                        result = row['evaluation'] or recommendation_evaluate(row['snapshot'])
                    else:
                        result = row['evaluation'] or evaluate(row['snapshot'])
                    c.execute("UPDATE recsys_ab.evaluation_outbox SET evaluation=%s WHERE request_key=%s", (Jsonb(result), row['request_key']))
                    if offline:
                        from jenkins.python.llm_agent_cd.offline_export import export
                        export(sync.client,row['snapshot'],row['metadata'])
                    # Attempt every deterministic score in this record before
                    # yielding to Langfuse's asynchronous indexing.  Stopping
                    # at the first invisible score makes a record advance by
                    # only one metric per CronJob tick even though all score
                    # IDs are independent and idempotent.
                    pending = False
                    for payload in score_payloads(result, row['metadata']):
                        try:
                            sync.confirm(payload)
                        except ValueError as exc:
                            if str(exc) != 'score_not_visible':
                                raise
                            pending = True
                    if pending:
                        raise ValueError('score_not_visible')
                    c.execute("UPDATE recsys_ab.evaluation_outbox SET confirmed_at=now(),last_error=NULL WHERE request_key=%s", (row['request_key'],))
                    processed += 1
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    # Neither raw response text nor payloads/credentials in logs.
                    c.execute("""UPDATE recsys_ab.evaluation_outbox SET attempts=attempts+1,
                        last_error='evaluation_or_score_sync_pending',next_attempt_at=now()+interval '1 minute'
                        WHERE request_key=%s""", (row['request_key'],))
        finally:
            c.execute("SELECT pg_advisory_unlock(hashtextextended('recsys-recommendation-evaluation',0))")
    return processed


def main():
    db = Database(os.environ['AB_DATABASE_URL'])
    with httpx.Client(base_url=os.environ['LANGFUSE_BASE_URL'].rstrip('/'),
                      auth=(os.environ['LANGFUSE_PUBLIC_KEY'], os.environ['LANGFUSE_SECRET_KEY']),
                      timeout=10, follow_redirects=False) as client:
        print('confirmed_evaluations', drain(db, ScoreSync(client)))


if __name__ == "__main__":
    main()
