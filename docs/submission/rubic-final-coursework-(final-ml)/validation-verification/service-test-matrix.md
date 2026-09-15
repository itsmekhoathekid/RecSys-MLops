# Split Serving Services — Validation Matrix

Verified on 2026-08-30. Percentages and mutation gates are independent per
service.

| Requirement | Inference | Online Feature | RAG |
| --- | ---: | ---: | ---: |
| FastAPI `TestClient`, fixture/mock | PASS | PASS | PASS |
| Coverage `>90%` | 98.44% | 92.39% | 94.20% |
| Full EP/BVA | PASS | PASS, POST+GET | PASS, all 3 endpoints |
| HTTP Hypothesis idempotency | 60×3 | POST 60×3, GET 60×3 | 3 endpoints, each 60×3 |
| Mutation `>80%` | 724/819 = 88.40% | 189/226 = 83.63% | 691/785 = 88.03% |
| Bad selected mutant states | 0 | 0 | 0 |
| Locust evidence | PASS | PASS | PASS |

## Source map

| Service | Validation suite | Mutation oracle |
| --- | --- | --- |
| Inference | [`inference_api/test_validation_design.py`](../../../../tests/unit/api_serving/inference_api/test_validation_design.py) | [`inference_api/test_mutation_oracles.py`](../../../../tests/unit/api_serving/inference_api/test_mutation_oracles.py) |
| Online Feature | [`online_feature_api/test_validation_design.py`](../../../../tests/unit/api_serving/online_feature_api/test_validation_design.py) | [`online_feature_api/test_mutation_oracles.py`](../../../../tests/unit/api_serving/online_feature_api/test_mutation_oracles.py) |
| RAG | [`rag_api/test_validation_design.py`](../../../../tests/unit/api_serving/rag_api/test_validation_design.py) | [`rag_api/test_mutation_oracles.py`](../../../../tests/unit/api_serving/rag_api/test_mutation_oracles.py) plus direct retrieval/chunk unit tests |

Root [`pyproject.toml`](../../../../pyproject.toml) is the only Mutmut config.
[`run_mutation.py`](../../../../tests/unit/api_serving/run_mutation.py) supports `inference`,
`online-feature`, `rag`, and `all`; it writes JSON/text reports and fails when a
score is not strictly greater than 80% or any selected mutant has a bad state.

```bash
UV_CACHE_DIR=.uv-cache RECSYS_OTEL_ENABLED=0 \
uv run python tests/unit/api_serving/run_mutation.py all --max-children 8
```

The standalone
[`ServingMutation.Jenkinsfile`](../../../../jenkins/ServingMutation.Jenkinsfile)
runs this gate nightly or manually. The main push/PR pipeline retains fast
unit, contract, EP/BVA, idempotency and coverage feedback.

## Fifteen-capture contract

Each service has five captures: coverage+fixture/mock, EP/BVA, mutation,
idempotency, and Locust. Commands and fixed filenames are in the two main docs:

- [ML Serving Validation](../validation_verification.md)
- [RAG Validation](<../../rubric-final-coursework-(final-llm)/validation_verification.md>)
