# Coordinator-only Qwen2.5 production preflight — 2026-09-11

## Scope

This run changed only the Coordinator experiment axis. The control remained
workflow release
`2458bfb95a8e3310adeea29ab39fe2f5ebf963d4f48060356fdc8c8fb84385c6`.
The candidate used Qwen2.5 for Coordinator while Context and Recommendation
retained the control LLM identity, effective generation configuration, prompts,
tools and backend bindings.

No experiment was created and no A/B traffic was opened. The three calls below
were create-only infrastructure preflights and do not count toward the offline
six-call suite or the online twenty-case suite.

## Capacity change

Jenkins job `RecSys-Workflow-Coordinator-Worker-Capacity` build 1 succeeded.
It changed only `recsys-coordinator-sandbox-pool` from `1/1` to `2/2` through
its KEDA `ScaledObject`. Context and Recommendation remained `1/1`.

The job held `recsys-production-release` and `recsys-workflow-state`, wrote the
capacity intent before mutation, included the exact future candidate and its
adapters in the scheduling preflight, and emitted zero inference requests.

## Candidate preflight

Jenkins job `RecSys-Workflow-Coordinator-Candidate-Preflight` build 1 deployed
candidate release
`07fd1840199204346d4ad5e13a2741a0653d89b17b05efc56c9bc3116a307b8e`
at weight zero. All immutable resources reached Ready before the probes.

The exact three-call matrix failed without retry:

| Probe | Result | Evidence |
|---|---|---|
| Recommendation A2A | FAIL | `ReadTimeout` after the 120-second wall-clock limit |
| Context A2A | FAIL | `ReadTimeout` after the 120-second wall-clock limit |
| Composite A2A | FAIL | Substrate rejected execution because both Coordinator workers were occupied |

The first two independent routes both remained active after their specialist
result instead of producing the terminal Coordinator response. The composite
request then received `substrate worker pool has no free workers`. Increasing
the pool therefore removed the earlier one-worker ambiguity and proved that
worker exhaustion was a consequence of the terminal loop, not its cause.

The hard preflight gate prevented canary, offline evaluation, the online
twenty-case fixture and promotion. Online A/B request count is zero.

## Quarantine and final production state

Jenkins job `RecSys-Workflow-Failed-Coordinator-Candidate-Quarantine` build 1
succeeded. Both candidate adapters are at zero replicas and the candidate
Coordinator `SandboxAgent` was removed after the two timed-out tasks became
quiescent. The following evidence and recovery material remain intact:

- Qwen2.5 backend and immutable ModelConfig;
- unchanged candidate specialist resources;
- TaskStore history and create-only MinIO intents/results;
- Jenkins artifacts and the journaled pre-mutation snapshot.

Final verified state:

- Coordinator workers: `2/2`; Context: `1/1`; Recommendation: `1/1`.
- Qwen2.5 backend: `1/1 Ready`; candidate adapters: `0/0`.
- Istio allocation: 100% to control release `2458bfb95a8e3310adee…`.
- Langfuse dispatch: `false`.
- Workflow state/champion: unchanged; no experiment ID active.
- Canary, online A/B and promotion: not started.

## Code verification

The Coordinator-only release identity and specialist-freeze guards, capacity
job, preflight job and exact quarantine job passed the relevant automated suite:
`372 passed, 28 skipped`. `git diff --check` passed.
