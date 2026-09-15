# Recommendation A/B through the production A2A edge

The single Recommendation Traffic Job sends operational requests and its 20
frozen cases through the public production path. Jenkins sends no traffic:

```text
Traffic Job -> agents.recsys-mlops.site -> TLS/NGINX Basic Auth
            -> recsys-ab-edge -> recsys-ab-router -> Istio -> control/candidate
```

This applies only when the immutable experiment policy contains
`"synthetic_entrypoint": "public_a2a"`. Existing states without that field keep
the legacy internal entrypoint and can be resumed safely.

## Security and exactly-once behavior

The Traffic Job creates HMAC-SHA256 tickets with a fixed 65-minute TTL. A ticket is
bound to the experiment, case, deterministic request ID, fixture checksum,
prompt SHA256, traffic kind, phase and a random nonce. `synthetic_case` is only
valid in AB; `live_test` is valid in CANARY/AB/VERIFY. Tickets contain neither
expected output nor a credential. NGINX Basic Auth and a valid ticket are both
required.

Before each HTTPS call, the Traffic Job persists `DISPATCH_INTENT`. The edge atomically
changes that row to `CLAIMED` before forwarding it. A terminal response changes
it to `COMPLETED`; a malformed request is `REJECTED`; an uncertain network or
upstream outcome is `AMBIGUOUS`. None of these cases can be replayed, retried or
replaced.

Client-provided source, release, variant, experiment and user headers are never
forwarded. The edge assigns the verified identity `synthetic`; the private
router still checks the active AB phase and exact frozen fixture.

## Production enablement order

1. Run the additive database migration by starting the new router image.
2. Add `AB_CASE_TICKET_KEY` with the same random value to
   `kagent/recsys-llm-ab-runtime` and `ci/recsys-llm-ab-cd`.
3. Create Jenkins username/password credential `recsys-agents-edge-auth` for
   the existing production gateway account.
4. Deploy `recsys-llm-ab` with `values-prod.yaml`; verify both edge pods Ready.
5. Verify public TLS, unauthenticated `401`, and authenticated invalid-ticket
   `403`. The private invocation count must remain unchanged.
6. Update the managed Jenkins executor and provision the dashboard.
7. Only then accept a new Langfuse `ab-ready` version. The Recommendation policy
   selects `public_a2a`; no active/champion route is changed by edge deployment.

Rollback of the entrypoint is a new policy with `synthetic_entrypoint=internal`
plus disabling `externalEdge`. It does not mutate champion, previous, Istio
weights or historical ticket/evaluation records.

## Gate evidence

Promotion requires one 20-row ticket suite, 20 `COMPLETED` tickets, 20 terminal
root invocations, 20 confirmed Langfuse evaluations, 20 functional PASS results
and the existing live-test operational gates. Any missing or ambiguous public
request remains HOLD until stage timeout and then rolls the route back.

The Grafana dashboard exposes `Public A2A 20/20`, ticket states, bounded
transport-error families, actual control/candidate split and deterministic raw
functional metrics. Full error text, ticket values and request/session IDs are
never Prometheus labels.
