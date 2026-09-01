# Validation & Verification Evidence

## Current split-service result (2026-08-30)

All five rubric rows now have passing evidence for Inference API, Online Feature
API, and RAG Feature API. The authoritative matrix, commands, test-design map,
and current measurements are in
[service-test-matrix.md](service-test-matrix.md).

| Rubric evidence | Inference API | Online Feature API | RAG Feature API |
| --- | ---: | ---: | ---: |
| Unit/Web API tests | PASS | PASS | PASS |
| Line coverage `> 90%` | 98.44% | 92.39% | 94.20% |
| EP/BVA parametrization | PASS | PASS | PASS |
| Full public-path mutation score `> 80%` | 88.40% | 84.91% | 87.19% |
| Hypothesis idempotency | PASS | PASS | PASS |
| Locust + HTML SLA evidence | PASS | PASS | PASS |

The sections below preserve the original coursework evidence and therefore use
the older monolithic component names and measurements.

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

- Web API suites use real `TestClient`, service fixtures and injected mocks under [`tests/unit/api_serving`](../../../../tests/unit/api_serving/).
- EP/BVA cases are visible in the three service `test_validation_design.py` files.
- HTTP idempotency uses Hypothesis with 60 examples and three requests per example.
- Mutation oracles are centralized under [`tests/mutation/api_serving`](../../../../tests/mutation/api_serving/).
- Locust HTML SLA report: archived `locust-api.html`.

The original one-off evidence, mutation and load runners were removed from the
production Jenkins script tree. This directory preserves their immutable
coursework output only.

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
