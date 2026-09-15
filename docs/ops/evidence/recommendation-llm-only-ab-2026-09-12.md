# Recommendation LLM-only A/B production evidence

- Experiment: `rec-b421cd5867229c2813811c691e7e96e5`
- Jenkins: `RecSys-LLM-Agent-CD #38` — `SUCCESS`
- Final state: `COMPLETED`, gate `PASS`
- Champion: Qwen2.5-0.5B-Instruct Q4_K_M release `cf45f5192429376fa7567be7c5b9bdf7cbff7f6e1f3268c19cf011982bb959d3`
- Previous: Qwen3.5-0.8B Q4_0 release `3820e879a0cae73e2ee16d1f550fdfc178ef4237e8149b7dd50da0fdaa03b91a`
- Istio: verified 100% of new Recommendation sessions to the champion, route revision `07d86091e2f8e243779c77f7cb9e12588a50431908af2e67415f64b8cfde9875`

The offline compatibility gate executed exactly six inferences: three cases on
each model, all passing. The online functional gate executed exactly twenty new
root conversations: 11 control and 9 candidate, all passing. All 20 online
records were scored by `recommendation-code-v2`; the complete experiment has
26/26 confirmed Langfuse records (six offline plus twenty online), 286 score
rows, and no failed evaluation.

Closed operational windows:

| Stage | Control | Candidate | Control p95 | Candidate p95 | Verdict |
|---|---:|---:|---:|---:|---|
| 10% canary | 81 | 5 | 11.919 s | 6.809 s | PASS |
| 50% A/B | 12 | 8 | 11.935 s | 3.936 s | PASS |
| 100% verify | 0 | 60 | n/a | 3.904 s | PASS |

Production uses the upstream kagent `0.10.0-rc1` chart, upstream Substrate
`0.0.9`, `kagent.dev/v1alpha2` SandboxAgents, and the digest-pinned upstream Go
ADK image. The retired custom Go ADK/kagent/Substrate patches, build scripts,
post-renderer, and mTLS bootstrap chart are not part of the runtime or repository
target. The Python A/B router, deterministic result adapter, evaluator, Jenkins,
Istio, and Langfuse integration remain as external release infrastructure.

Post-promotion monitoring is disabled by policy. Manual rollback remains
available through the stored `previous` pointer.

After promotion, `RecSys-Global-Model-Config #4` ran the unchanged global
defaults through the shared Jenkins lock and completed successfully. The
legacy Helm release record was migrated metadata-only from removed
`kagent.dev/v1alpha3` to upstream `v1alpha2`. All three default SandboxAgents
were Ready with no custom revision annotation or prompt marker. The A/B state
ETag, champion, previous pointer, route revision, and 100% Istio weight were
unchanged after this normal deploy.
