# AI observability production proof — 2026-08-31

- GKE context: `recsys-mlops-gke`
- Helm revisions: observability `20`, security `4`, kagent `28`
- Terraform audit: targeted plan for observability, security and kagent reports `No changes`
- Acceptance: `39/39` checks and `51/51` required Prometheus-backed panels passed
- Probe: success `1`, TTFT `0.257250237s`, round trip `0.304641983s`, tokens `16/2/18`
- Agent calls: coordinator `1`, context `2`, recommendation `2`
- Safety detections: email `1`, phone `3`, payment card `3`, prompt injection `1`
- Tempo: four matching kagent traces, zero raw synthetic PII matches
- Loki: zero raw synthetic email, phone or payment-card matches in the post-traffic proof window

Known proof limitation: the recommendation MCP returns `isError=true` for an
invalid schema before entering the registered tool handler, so its native
handler counter does not increment for that protocol-level rejection. The
feature MCP failure counter and the aggregate MCP failure panels are non-empty;
counting pre-handler recommendation validation errors would require transport
instrumentation and is intentionally not added here because it could alter the
production streaming request path.

Evidence files:

- `validation-report.json`: timestamped query/value/pass evidence and panel checks
- `prometheus-targets.json`: Prometheus target-health snapshot

Dashboard screenshots could not be captured in this run because the in-app browser's
URL security policy blocked further interaction with the authenticated localhost
Grafana page. No alternate browser or CDP workaround was used.
