# llm-d benchmark profiles

The repository deploys the stack with Terraform and Helm, so benchmarks use
`llmdbenchmark` in run-only mode against the existing agentgateway endpoint.

Start with the upstream `sanity_random.yaml` workload:

```bash
make llm-inference-benchmark
```

Override the workload or endpoint without editing the script:

```bash
LLMDBENCH_WORKLOAD=shared_prefix.yaml \
LLMDBENCH_ENDPOINT_URL=http://GATEWAY_IP:80 \
make llm-inference-benchmark
```

Keep each run in a separate `reports/llm-d/<UTC timestamp>` workspace. Compare
TTFT, inter-token latency, output-token throughput, request throughput and
error rate using the same prompt distribution and concurrency.
