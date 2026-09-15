# Recommendation LLM A/B poller

The Recommendation A/B trigger is a Kubernetes CronJob and the only rollout
controller. The workflow-scoped Langfuse webhook is independent and unchanged.
Jenkins executes one controller-authorized action per build; it never polls,
creates traffic, or selects the next transition.

## Register an LLM release

```bash
uv run python -m jenkins.python.llm_agent_cd.llm_ab_start \
  --scope recommendation \
  --model-alias qwen35-0.8b-q4
```

The short alias resolves only to the reviewed repository, revision, GGUF file,
SHA256, size and serving profile compiled into the CLI. The command verifies
that identity against Hugging Face, performs a create-only MinIO catalog write,
checks read access with the poller credential, and emits a complete
`langfuse_config`. It does not download, deploy or invoke the model. The verbose
allowlisted arguments remain available for automation compatibility, but cannot
select an unreviewed tuple.

If the resolved candidate release previously failed a hard gate, the command
returns `status=QUARANTINED` without `langfuse_config`. A new prompt version or
another `ab-ready` assignment cannot bypass release quarantine; a reviewed new
alias/profile revision is required.

Create an immutable `recsys-recommendation-ab` prompt version with that config,
then move the `ab-ready` label to it. The poller claims it by moving
`ab-running` to the candidate and returning `ab-ready` to the non-runnable
parking version. The controller dispatches separate `prepare`, `route-10`,
`route-50`, `route-100`, `promote`, and `cleanup` Jenkins builds. A terminal,
cleaned run moves `ab-done` or `ab-fail` to the candidate and returns
`ab-running` to parking.

Start the single traffic producer after the poller exposes the experiment ID:

```bash
uv run python -m jenkins.python.llm_agent_cd.llm_ab_traffic start \
  --experiment-id rec-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  --entrypoint public-a2a \
  --follow
```

The command is create-only. Re-running it attaches to the existing Job. The Job
emits bounded `live_test` traffic in CANARY/VERIFY and the exact 20 signed cases
in AB; it cannot call Jenkins or modify Istio. Without this command the
controller records `HOLD: awaiting traffic` and rolls back at the stage timeout.

## Production cutover

1. Deploy the chart with `trigger.suspendPolling=true` and run the additive DB
   migration with `python -m apps.agentic.llm_ab_router.trigger migrate` using
   the scoped poller secret.
2. Run `recommendation_langfuse_automation deactivate`.
3. Run `recommendation_langfuse_automation parking`, then configure the scoped
   poller secret with `recommendation_trigger_credentials`.
4. Move the old `ab-ready` pointer to parking using
   `recommendation_langfuse_automation park-ready` after reconciling any prior
   terminal result.
5. Unsuspend `recsys-recommendation-ab-poller` and verify two successful Jobs.

Do not delete `recsys-recommendation-webhook-auth` during the rollback window.
Rollback consists of suspending the poller and restoring the previous chart;
it must not re-submit any `DISPATCHING` or `NEEDS_ATTENTION` request.

## Terminal cleanup

Only the controller dispatches `ACTION=cleanup`, after a verified `COMPLETED`
or `ROLLED_BACK` state. Cleanup is not hidden in `post.always` and does not run
after an unverified rollback.

Cleanup is a restartable reconciliation, not `kubectl delete pod`. It writes a
CAS-protected intent, closes completed `synthetic` and `live_test` sessions,
closes sessions pinned to a quarantined release, and conservatively retains
unknown, production and unfinished sessions. It then removes unreachable
release pins from the generated VirtualService, verifies every ready Envoy,
and only afterward scales controller-owned adapters and unreferenced managed
LLM backends to zero. Services, ModelConfigs, SandboxAgents, MinIO state,
PostgreSQL rows and Langfuse evidence remain available for audit or recovery.
The state keeps a durable cleanup ownership registry, so pruning a release
from active routing does not make a later cleanup retry lose authority over
the corresponding scaled-down Deployment.

The action is safe to rerun after a Jenkins restart. `ROLLBACK_FAILED` blocks
cleanup because its route is not verified, and a new A/B request cannot start
while an existing cleanup intent is incomplete.
