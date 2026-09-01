# Locust RAG API SLA

- Endpoint: `POST /v1/rag/retrieve`
- Exported requests: 922
- Failures: 0
- Failure rate: 0.00%
- Throughput: 102.25 req/s
- p95 latency: 7 ms
- SLA: failures = 0, p95 < 1,000 ms, throughput >= 5 req/s
- Result: PASS

The reproducible runner is
[`tests/load/run_rag_load_proof.sh`](../../../../../tests/load/run_rag_load_proof.sh).
