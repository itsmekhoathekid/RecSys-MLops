# Validation & Verification Evidence

## Coverage

| Component | Line coverage |
| --- | ---: |
| api | 95.44% |
| dp1 | 97.27% |
| dp2 | 95.12% |
| dp3 | 97.40% |
| drift | 99.00% |
| kserve | 90.42% |
| materialize | 98.04% |
| spark_batch | 95.12% |
| stream_offline | 96.12% |
| stream_online | 97.58% |
| training | 98.31% |

## Required Proof

- Web API unit tests use `TestClient`, pytest fixtures, and mocked Redis/Triton services.
- EP/BVA cases are visible in pytest IDs containing `equivalence-*` and `boundary-*`.
- Property-based idempotency uses Hypothesis in `tests/unit/api_serving/test_validation_verification.py`.
- Mutation score: 90.74%.
- Locust HTML SLA report: `locust-api.html` after `validation_load_test.sh` runs.

## Commands

```bash
COVERAGE_MIN=90 UV_CACHE_DIR=.uv-cache bash jenkins/scripts/component_ci.sh api
MUTATION_TARGETS='apps/api-serving/src/ranking.py apps/api-serving/src/online_features.py' MUTATION_MUTANT_NAMES='ranking.x_format_top_k* online_features.x_get_online_features*' UV_CACHE_DIR=.uv-cache bash jenkins/scripts/validation_mutation.sh
RECSYS_LOAD_HOST=http://127.0.0.1:8088 UV_CACHE_DIR=.uv-cache bash jenkins/scripts/validation_load_test.sh
bash jenkins/scripts/validation_evidence.sh
```

## Screenshot Checklist

- `screenshots/coverage-api.png`: terminal coverage output showing `>90%`.
- `screenshots/fixtures-mocks-web-api.png`: pytest output for `TestClient` + fixture/mock tests.
- `screenshots/ep-bva-parametrize.png`: pytest output with `equivalence-*` and `boundary-*` case IDs.
- `screenshots/mutation-score.png`: mutation summary showing score `>80%`.
- `screenshots/property-idempotency.png`: Hypothesis idempotency test output.
- `screenshots/locust-html-sla.png`: opened `locust-api.html` report.

## Mutation Summary

# Mutation Testing

- Mutation score: 90.74%
- Gate: > 80.00%
- Killed: 49
- Survived: 5
- Timeout: 0
- Suspicious: 0
- No tests: 0
- Targets: apps/api-serving/src/ranking.py, apps/api-serving/src/online_features.py
- Mutant filters: ranking.x_format_top_k*, online_features.x_get_online_features*


## Locust Summary

# Locust Web API SLA

- Host: Aggregated
- Requests: 729
- Failures: 0
- Failure rate: 0.00%
- Throughput: 38.33 req/s
- p95 latency: 39.00 ms
- SLA: failure rate 0%, p95 < 1000 ms, throughput >= 5 req/s
- Result: PASS
