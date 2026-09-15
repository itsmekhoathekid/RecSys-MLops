# Serving-format diagnostic — 10 September 2026

Status: **native-tool remediation implemented and tested locally; build/deploy and
production A/B remain incomplete**.

The approved Jenkins dispatch ACL was reverified: root/queue/Workflow-CD reads
returned 200, Workflow-CD configuration and script access returned 403, and the
unrelated CICD job returned 404. No account permissions were expanded in this run.

## Compatibility evidence

Seven bounded, create-only inference diagnostics ran on the existing control
server with fake tool declarations and no dependency executor. These are neither
the six offline acceptance calls nor the twenty synthetic workflow conversations.
No request was retried. Temperature 0.0, seed 42 and max_tokens 384 were retained.

| Input / serving option | Result |
| --- | --- |
| Short diagnostic prompt + simplified schema, default thinking | PASS |
| Same, thinking disabled | PASS |
| Failed runtime trace's prompt + real MCP schema, default thinking | FAIL, token limit |
| Same, thinking disabled | FAIL, token limit |
| Runtime prompt + simplified schema | FAIL, token limit |
| Short prompt + real MCP schema | PASS |
| Runtime prompt + real schema + required-tool format prefill | PASS, 46 generated tokens |

The failed request was reconstructed from the recorded GENERATION input in trace
`c6b6a4ed2be5c73655df716fbffb12ee`. No A2A task or tool execution was replayed.
The final diagnostic supplied only the XML opening for the tool already selected
by the runtime. The model generated the arguments, which matched user 218,
top_k 3 and null candidate_item_ids. Do not describe this as model tool-selection
accuracy: tool identity was supplied by the runtime.

The earlier missing-Jinja hypothesis is rejected: `common/common.h` at serving
revision `0b1bad14f` sets `use_jinja = true`. Adding the CLI flag would not repair
this failure. Disabling thinking also failed with the actual runtime prompt.
The Qwen native parser permits content before required tool calls; the diagnostic
does not establish whether a future upstream parser change alone fixes the model.

## Selected remediation

The follow-up follows the
[llama.cpp function-calling flow](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md)
and uses the [official Qwen3.5 tool-aware template](https://huggingface.co/Qwen/Qwen3.5-0.8B/blob/2b48083dfa97c3cbf0220cf1a5e6cffe0a511157/tokenizer_config.json)
from `Qwen/Qwen3.5-0.8B` revision
`2b48083dfa97c3cbf0220cf1a5e6cffe0a511157`, pinned by SHA256
`7738ebd6d0a355161b3e4a80f9d1913a12f8c39e2ff0a89775be0c86a02e85a7`.
It creates a new `qwen35-native-tools-v1` LLM identity and dedicated backend;
the legacy shared backend is not edited. The new profile has no reasoning-budget
or reasoning-budget message. Readiness verifies the template selected from
`chat_template_tool_use`, falling back to the explicitly mounted default template,
and requires native tools/tool-calls capabilities.

Execution policy revision 3 restricts each turn to the next trajectory tool and
normalizes its schema for strict function calling: all object properties are
required, formerly optional properties become nullable, every object rejects
additional properties, defaults/titles are removed, and the OpenAI-compatible
wire includes `strict:true`. Argument validation and the durable pre-execution
claim remain authoritative; the evaluator does not repair outputs.

Local verification: 296 Jenkins/A-B unit tests, 27 PostgreSQL integration tests,
the complete Go model/agent packages, and the database-backed guard/no-replay
tests passed. The disposable local PostgreSQL container and its anonymous volume
were removed afterward.

## Safe deployment boundary

Do not insert a fake function result, copy expected arguments into generated
output, shorten the business prompt, or increase the generation budget to force
PASS. Do not patch the shared serving Deployment in place and retain the old LLM
identity. No such changes were made here.

Cloud Build was not started: the execution safety reviewer requires explicit
authorization to upload the internal runtime source tree to the external GCP
Cloud Build project. Do not route around that block with a different upload.
After approval, build the pinned runtime, deploy the dedicated backend without
traffic, attest its live template, run the bounded workflow serving checks, and
only then cut over a new baseline under the shared Jenkins lock. Capacity remains
a separate hard gate before a new A/B experiment.

The diagnostics add no inference-serving pod, change no champion/route, and do
not enable dispatch or claim end-to-end completion. Four diagnostic unit tests
pass. Redacted results and raw evidence checksums are in
[the evidence manifest](evidence/workflow-serving-diagnostic-2026-09-10.json).
Raw create-only request/response files remain under the four
`/private/tmp/recsys-forced-tool-diagnostic-20260910-*` directories.
