# Recommendation LLM model onboarding

This flow prepares a public, revision-pinned GGUF model at zero production
traffic before it can be selected by the Langfuse A/B poller. It does not
modify kagent or Go ADK sources and it does not create a Langfuse prompt.

## Run

Use an immutable alias for each reviewed artifact/profile pair:

```bash
RUN_DIR="$(mktemp -d /tmp/recsys-ab.XXXXXX)"

uv run python -m jenkins.python.llm_agent_cd.llm_ab_start onboard \
  --scope recommendation \
  --model-alias new-model-q4-v1 \
  --artifact-url \
    'https://huggingface.co/org/repo/resolve/0123456789abcdef0123456789abcdef01234567/model-Q4_K_M.gguf' \
  --output-dir "$RUN_DIR"

jq . "$RUN_DIR/registration.json"
jq '.langfuse_config' "$RUN_DIR/registration.json" | pbcopy
```

The command inspects only bounded GGUF metadata locally. Jenkins downloads the
complete weight on GKE, verifies its LFS SHA-256, starts the policy-pinned
llama.cpp image, verifies native tool capability, and runs six fake-tool
compatibility requests. A successful result has `status: READY`, a candidate
with traffic weight zero, and a `langfuse_config` ready to paste.

Paste only `langfuse_config` into a new immutable Recommendation prompt version
in Langfuse. Assign `ab-ready` manually. The one-minute poller then starts the
normal A/B pipeline: offline 3×2, canary 10%, 20 public-edge cases at 50%,
verify 100%, and promote or rollback.

## Observe and recover

```bash
# Jenkins onboarding stage and console
kubectl -n ci port-forward service/recsys-jenkins 8080:8080

# Candidate download/readiness and six-case Job
kubectl -n kagent get pods,jobs -l recsys.ai/owner=llm-agent-cd
kubectl -n kagent logs job/llm-onboard-<32-hex-onboarding-id>

# Poller claim, A/B dispatch, and two-hour cleanup
kubectl -n kagent logs job/$(kubectl -n kagent get jobs \
  -l app=recsys-recommendation-ab-poller \
  -o jsonpath='{.items[-1:].metadata.name}')
```

Do not retry a failed compatibility Job or reuse a blocked alias. The database
claims every case before inference, so an ambiguous crash is intentionally a
failure rather than a hidden second request. An unused prepared candidate is
scaled to zero after two hours; its catalog, alias, and evidence remain.
