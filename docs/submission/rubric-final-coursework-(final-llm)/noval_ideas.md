# Novel ideas

## Controller-driven LLM A/B delivery

The Recommendation LLM rollout uses a controller pattern instead of a long-running Jenkins pipeline. A Kubernetes CronJob evaluates immutable operational and functional evidence, Jenkins executes exactly one authorized mutation per build, and a separate durable Traffic Job sends signed requests through the real production edge. This separation makes route decisions idempotent, auditable, fail-closed, and independently observable.

The implementation also combines immutable LLM release identities, session-sticky Istio allocation, exact-20 public A2A evaluation without an LLM judge, Langfuse lifecycle pointers, Envoy data-plane verification, and terminal capacity cleanup.

See the complete architecture, commands, code references, gate definitions, screenshots, and troubleshooting procedure in [Recommendation LLM A/B Rollout — Production Runbook](./a_b.md).
