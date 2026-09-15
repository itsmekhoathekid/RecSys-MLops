# Template compatibility investigation — 2026-09-09

Status: **HOLD for workflow A/B; zero A/B conversations executed**.

The live `/props` endpoint confirmed that the pinned Qwen2.5 GGUF embeds a
tool-call example with doubled literal braces. Added a checksum-bound v2
serving profile that corrects only these braces via an immutable ConfigMap.
Generation parameters, business prompts, fake tool schemas and all six
compatibility assertions were unchanged. The original v1 catalog is untouched.

New diagnostic LLM identity:
`de1d2557a58d6e1114497dec8b77cf259bd6953ffb5bba27b3a9c395abd6c3ae`.
Template SHA256:
`cd8e9439f0570856fd70470bf8889ebd8b5d1107207f67a5efb46e342330527f`.

Deployed the v2 backend alone on the CPU-services node, using the existing
pinned llama.cpp image. Ran exactly six new diagnostic inference requests,
without HTTP retries or real tool execution. Result remains **3/6**:

- Tool selection, explicit arguments and composite next step pass.
- Missing user ID still fabricates `user_id=123` and calls a tool.
- Empty result invents an unavailable-user explanation.
- Tool result is prose rather than the JSON required by this diagnostic.

Therefore the serialization defect is real but does not explain away the
behavioral failures. The profile is **not accepted**, and its catalog was not
published to MinIO. Both v1 and v2 diagnostic Deployments are at zero replicas;
Services and the immutable template ConfigMap remain for audit. No champion,
Istio route, workflow activation or dispatch setting changed. Offline
Airflow/DataHub/MLflow remain stopped by user instruction.

An additional blocker is the existing workflow evaluator's exact-JSON outcome
requirement: live business prompts ask for concise text. Do not solve this by
accepting arbitrary prose, matching a few IDs, or dropping metadata/grounding
assertions. A revised, explicitly agreed output contract / deterministic
evidence renderer or a rigorously tested prose evaluator is needed before
claiming workflow acceptance. Changes to business prompts require a new baseline
before config-only or LLM-only comparison, not a hidden challenger change.

Evidence: `evidence/small-model-template-v2-compatibility-2026-09-09.json`.
Raw six responses: `.llm-agent-cd/compat-small-20260909-template-v2-run1/`.
The global recovery journal records replica changes; do not run its blanket
restore, which would also restart services the user asked to keep stopped.
