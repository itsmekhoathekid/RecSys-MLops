# Validation & Verification

## 1. Unit Test Coverage > 90%

### 1.1 Goal

- Requirement: unit tests must pass with line coverage `> 90%`.
- Scope: `api-serving` unit tests over the full `apps/api-serving/src` package.
- Current result: `29 passed`, `1 warning`, total line coverage `92.25%`.
- Coverage gate: pytest uses `--cov-fail-under=90`.

Source references:

- [apps/api-serving/src/main.py line 73](../../../apps/api-serving/src/main.py#L73): `/healthz` endpoint covered by API surface tests.
- [apps/api-serving/src/main.py line 78](../../../apps/api-serving/src/main.py#L78): `/ready` endpoint and forced-not-ready branch.
- [apps/api-serving/src/main.py line 93](../../../apps/api-serving/src/main.py#L93): `/metrics` endpoint.
- [apps/api-serving/src/main.py line 98](../../../apps/api-serving/src/main.py#L98): `/online-features/{user_id}` endpoint.
- [apps/api-serving/src/main.py line 116](../../../apps/api-serving/src/main.py#L116): `POST /recommendations` endpoint.
- [tests/unit/api_serving/test_validation_verification.py line 107](../../../tests/unit/api_serving/test_validation_verification.py#L107): API health/readiness/version/metrics/online-features coverage test.
- [tests/unit/api_serving/test_validation_verification.py line 149](../../../tests/unit/api_serving/test_validation_verification.py#L149): API `502` error-path coverage test.
- [tests/unit/api_serving/test_validation_verification.py line 189](../../../tests/unit/api_serving/test_validation_verification.py#L189): singleton and env-helper coverage test.

### 1.2 Command used

```bash
UV_CACHE_DIR=.uv-cache PYTHONPATH=apps/api-serving/src \
uv run pytest tests/unit/api_serving -q \
  --cov=apps/api-serving/src \
  --cov-report=term-missing \
  --cov-fail-under=90
```

Expected terminal summary:

```text
29 passed, 1 warning
Required test coverage of 90% reached. Total coverage: 92.25%
```

### 1.3 Screenshot Proof

![Data & ML system](../../pngs/unit_test_90.png)

## 2. Web API Tests With Fixtures And Mocks

### 2.1 Goal

- Requirement: prove Web API unit tests use pytest fixtures and mocks.
- Web API endpoints under test: `POST /recommendations`, `GET /online-features/{user_id}`, `/healthz`, `/ready`, `/version`, and `/metrics`.
- External dependencies mocked: Redis/online feature store, Triton/KServe ranker, and model metadata/env config.

Source references:

- [apps/api-serving/src/main.py line 98](../../../apps/api-serving/src/main.py#L98): `GET /online-features/{user_id}` FastAPI route.
- [apps/api-serving/src/main.py line 116](../../../apps/api-serving/src/main.py#L116): `POST /recommendations` FastAPI route.
- [apps/api-serving/src/api_schemas.py line 8](../../../apps/api-serving/src/api_schemas.py#L8): `RecommendationRequest` validation model.
- [tests/unit/api_serving/test_validation_verification.py line 4](../../../tests/unit/api_serving/test_validation_verification.py#L4): FastAPI `TestClient` import.
- [tests/unit/api_serving/test_validation_verification.py line 43](../../../tests/unit/api_serving/test_validation_verification.py#L43): pytest fixture definition.
- [tests/unit/api_serving/test_validation_verification.py line 47](../../../tests/unit/api_serving/test_validation_verification.py#L47): feature client mock via `monkeypatch`.
- [tests/unit/api_serving/test_validation_verification.py line 48](../../../tests/unit/api_serving/test_validation_verification.py#L48): ranker mock via `monkeypatch`.
- [tests/unit/api_serving/test_validation_verification.py line 111](../../../tests/unit/api_serving/test_validation_verification.py#L111): model metadata/env config mock via `monkeypatch.setenv`.

### 2.2 Test design

| Test area | Fixture/mock used | Expected behavior | Evidence |
| --- | --- | --- | --- |
| FastAPI `/recommendations` endpoint | `deterministic_api` fixture returns `TestClient` | API returns HTTP 200 for valid recommendation payloads | [test_validation_verification.py line 68](../../../tests/unit/api_serving/test_validation_verification.py#L68) |
| FastAPI `/online-features/{user_id}` endpoint | `deterministic_api` fixture returns `TestClient` | API returns HTTP 200 and deterministic online features | [test_validation_verification.py line 107](../../../tests/unit/api_serving/test_validation_verification.py#L107) |
| Redis/online feature store | `DeterministicFeatureClient` replaces `feature_client()` | Tests do not require live Redis and still return candidates/features | [test_validation_verification.py line 13](../../../tests/unit/api_serving/test_validation_verification.py#L13) |
| Triton/KServe ranker | `DeterministicRanker` replaces `ranker()` | Tests do not require live Triton/KServe and return deterministic scores | [test_validation_verification.py line 35](../../../tests/unit/api_serving/test_validation_verification.py#L35) |
| API error handling | `BrokenFeatureClient` and `BrokenRanker` mocks | API returns HTTP 502 when feature store or ranker fails | [test_validation_verification.py line 149](../../../tests/unit/api_serving/test_validation_verification.py#L149) |
| Model metadata/env config | `MODEL_VERSION`, `FORCE_NOT_READY`, and helper env vars are set through `monkeypatch.setenv` | API exposes model version, readiness branch, and env helper fallback | [test_validation_verification.py line 111](../../../tests/unit/api_serving/test_validation_verification.py#L111), [test_validation_verification.py line 141](../../../tests/unit/api_serving/test_validation_verification.py#L141), [test_validation_verification.py line 189](../../../tests/unit/api_serving/test_validation_verification.py#L189) |

### 2.3 Command used

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/unit/api_serving -q -vv
```

This command proves the `api-serving` unit tests pass. It does not measure coverage; coverage is proven by the command in Section 1.2.

Expected terminal summary:

```text
29 passed, 1 warning
```

### 2.4 Screenshot Proof

![Data & ML system](../../pngs/feature_and_mock_test.png)

## 3. Equivalence Partitioning And Boundary Value Analysis

### 3.1 Goal

- Requirement: use equivalence partitioning and boundary value analysis when designing parametrized test cases.
- Input model or endpoint: `POST /recommendations` using `RecommendationRequest`.
- Main validation rules: `user_id >= 1`, `1 <= top_k <= 100`, and optional `candidate_item_ids` length `1..500`.

Source references:

- [apps/api-serving/src/api_schemas.py line 9](../../../apps/api-serving/src/api_schemas.py#L9): `user_id` lower bound.
- [apps/api-serving/src/api_schemas.py line 10](../../../apps/api-serving/src/api_schemas.py#L10): `candidate_item_ids` length bounds.
- [apps/api-serving/src/api_schemas.py line 11](../../../apps/api-serving/src/api_schemas.py#L11): `top_k` lower and upper bounds.
- [apps/api-serving/src/main.py line 116](../../../apps/api-serving/src/main.py#L116): `POST /recommendations` endpoint under test.
- [tests/unit/api_serving/test_validation_verification.py line 53](../../../tests/unit/api_serving/test_validation_verification.py#L53): parametrized valid EP/BVA cases.
- [tests/unit/api_serving/test_validation_verification.py line 81](../../../tests/unit/api_serving/test_validation_verification.py#L81): parametrized invalid boundary cases.

### 3.2 Equivalence partitioning cases

| Partition | Example input | Expected result | Test ID | Status |
| --- | --- | --- | --- | --- |
| Valid explicit candidate list | `user_id=42`, `candidate_item_ids=[101,102,103]`, `top_k=2` | HTTP 200 | `equivalence-valid-explicit-candidates` | PASS |
| Valid fallback candidates | `user_id=42`, no explicit candidates, `top_k=3` | HTTP 200 | `equivalence-valid-fallback-candidates` | PASS |
| Invalid user ID | `user_id=0` | HTTP 422 | `boundary-invalid-user-id-zero` | PASS |
| Invalid `top_k` | `top_k=0` or `top_k=101` | HTTP 422 | `boundary-invalid-top-k-*` | PASS |
| Invalid candidate list | empty list or 501 candidates | HTTP 422 | `boundary-invalid-candidates-*` | PASS |

### 3.3 Boundary value analysis cases

| Boundary | Value | Expected result | Test ID | Status |
| --- | ---: | --- | --- | --- |
| Minimum `user_id` | `1` | HTTP 200 | `boundary-min-user-top-k-and-one-candidate` | PASS |
| Invalid `user_id` below min | `0` | HTTP 422 | `boundary-invalid-user-id-zero` | PASS |
| Minimum `top_k` | `1` | HTTP 200 | `boundary-min-user-top-k-and-one-candidate` | PASS |
| Invalid `top_k` below min | `0` | HTTP 422 | `boundary-invalid-top-k-zero` | PASS |
| Maximum `top_k` | `100` | HTTP 200 | `boundary-max-top-k-and-max-candidates` | PASS |
| Invalid `top_k` above max | `101` | HTTP 422 | `boundary-invalid-top-k-above-max` | PASS |
| Minimum candidate list length | `1` | HTTP 200 | `boundary-min-user-top-k-and-one-candidate` | PASS |
| Empty candidate list | `0` | HTTP 422 | `boundary-invalid-empty-candidates` | PASS |
| Maximum candidate list length | `500` | HTTP 200 | `boundary-max-top-k-and-max-candidates` | PASS |
| Candidate list above max | `501` | HTTP 422 | `boundary-invalid-candidates-above-max` | PASS |

### 3.4 Command used

```bash
UV_CACHE_DIR=.uv-cache uv run pytest \
  tests/unit/api_serving/test_validation_verification.py \
  -q -vv
```

### 3.5 Screenshot Proof

![Data & ML system](../../pngs/boundary_analysis.png)

## 4. Mutation testing

### 4.1 Goal

- Requirement: use mutation testing to evaluate test effectiveness.
- Mutation score gate: `> 80%`.
- Mutate only changed code: mutation targets are either detected from changed files or explicitly provided through `MUTATION_TARGETS`.
- Target file(s): `apps/api-serving/src/ranking.py` and `apps/api-serving/src/online_features.py`.
- Target function pattern(s): `ranking.x_format_top_k*` and `online_features.x_get_online_features*`.

Source references:

- [pyproject.toml line 27](../../../pyproject.toml#L27): `mutmut` dependency.
- [jenkins/scripts/validation_mutation.sh line 7](../../../jenkins/scripts/validation_mutation.sh#L7): base ref for changed-file detection.
- [jenkins/scripts/validation_mutation.sh line 8](../../../jenkins/scripts/validation_mutation.sh#L8): mutation score threshold.
- [jenkins/scripts/validation_mutation.sh line 25](../../../jenkins/scripts/validation_mutation.sh#L25): optional `MUTATION_TARGETS` input.
- [jenkins/scripts/validation_mutation.sh line 27](../../../jenkins/scripts/validation_mutation.sh#L27): fallback to changed files from git diff.
- [jenkins/scripts/validation_mutation.sh line 116](../../../jenkins/scripts/validation_mutation.sh#L116): generated `mutmut` configuration.
- [jenkins/scripts/validation_mutation.sh line 132](../../../jenkins/scripts/validation_mutation.sh#L132): mutate only covered lines.
- [jenkins/scripts/validation_mutation.sh line 134](../../../jenkins/scripts/validation_mutation.sh#L134): `only_mutate` target restriction.
- [jenkins/scripts/validation_mutation.sh line 167](../../../jenkins/scripts/validation_mutation.sh#L167): detected mutant calculation.
- [jenkins/scripts/validation_mutation.sh line 169](../../../jenkins/scripts/validation_mutation.sh#L169): mutation score formula.

### 4.2 Command used

```bash
MUTATION_TARGETS='apps/api-serving/src/ranking.py apps/api-serving/src/online_features.py' \
MUTATION_MUTANT_NAMES='ranking.x_format_top_k* online_features.x_get_online_features*' \
UV_CACHE_DIR=.uv-cache \
bash jenkins/scripts/validation_mutation.sh
```

### 4.3 Mutation result

| Metric | Result |
| --- | ---: |
| Mutation score | `90.74%` |
| Gate | `> 80%` |
| Killed mutants | `49` |
| Survived mutants | `5` |
| Timeout mutants | `0` |
| Suspicious mutants | `0` |
| No-test mutants | `0` |

Evidence files:

- `validation-verification/mutation-summary.md`
- `validation-verification/mutation-results.txt`

Result references:

- [validation-verification/mutation-summary.md line 3](validation-verification/mutation-summary.md#L3): mutation score.
- [validation-verification/mutation-summary.md line 5](validation-verification/mutation-summary.md#L5): killed mutants.
- [validation-verification/mutation-summary.md line 6](validation-verification/mutation-summary.md#L6): survived mutants.
- [validation-verification/mutation-summary.md line 10](validation-verification/mutation-summary.md#L10): mutation target file.

### 4.4 Screenshot Proof

![Data & ML system](../../pngs/mutation_testing.png)

## 5. Property-based idempotency testing

### 5.1 Goal

- Requirement: use property-based testing to verify idempotency.
- Property under test: repeated deterministic recommendation predictions for the same request return identical output.
- Generated inputs: `user_id`, `top_k`, and `candidate_item_ids`.
- Deterministic dependencies: deterministic feature client and ranker replace Redis and Triton/KServe.

Source references:

- [pyproject.toml line 26](../../../pyproject.toml#L26): `hypothesis` dependency.
- [tests/unit/api_serving/test_validation_verification.py line 5](../../../tests/unit/api_serving/test_validation_verification.py#L5): Hypothesis imports.
- [tests/unit/api_serving/test_validation_verification.py line 219](../../../tests/unit/api_serving/test_validation_verification.py#L219): `@given` property-based test.
- [tests/unit/api_serving/test_validation_verification.py line 220](../../../tests/unit/api_serving/test_validation_verification.py#L220): generated `user_id` strategy.
- [tests/unit/api_serving/test_validation_verification.py line 221](../../../tests/unit/api_serving/test_validation_verification.py#L221): generated `top_k` strategy.
- [tests/unit/api_serving/test_validation_verification.py line 222](../../../tests/unit/api_serving/test_validation_verification.py#L222): generated candidate list strategy.
- [tests/unit/api_serving/test_validation_verification.py line 242](../../../tests/unit/api_serving/test_validation_verification.py#L242): repeated predictions for the same input.
- [tests/unit/api_serving/test_validation_verification.py line 252](../../../tests/unit/api_serving/test_validation_verification.py#L252): idempotency assertion.

### 5.2 Property definition

For the same recommendation request and deterministic mocked feature/ranker
dependencies, repeated predictions must return the same:

- item order
- scores
- model version
- response metadata

### 5.3 Command used

```bash
UV_CACHE_DIR=.uv-cache uv run pytest \
  tests/unit/api_serving/test_validation_verification.py::test_property_based_recommendation_idempotency_for_deterministic_prediction \
  -q -vv
```

### 5.4 Result

| Field | Value |
| --- | --- |
| Library | Hypothesis |
| Number of examples | `60` |
| Result | PASS |
| Evidence | [test_validation_verification.py line 228](../../../tests/unit/api_serving/test_validation_verification.py#L228) |

### 5.5 Screenshot Proof

![Data & ML system](../../pngs/idempotency_testing.png)

## 6. Web API load testing with Locust

### 6.1 Goal

- Requirement: load test the Web API and produce an HTML report with SLA summary.
- Endpoint: `POST /recommendations`.
- Deployment target: `svc/recsys-api-serving` through local `kubectl port-forward` to `127.0.0.1:8088`.
- SLA: failure rate `0%`, throughput `>= 5 req/s`, and p95 latency `< 1000 ms`.

Source references:

- [pyproject.toml line 28](../../../pyproject.toml#L28): `locust` dependency.
- [tests/load/locustfile_serving.py line 27](../../../tests/load/locustfile_serving.py#L27): Locust user class.
- [tests/load/locustfile_serving.py line 36](../../../tests/load/locustfile_serving.py#L36): Locust task.
- [tests/load/locustfile_serving.py line 43](../../../tests/load/locustfile_serving.py#L43): Web API recommendation request builder.
- [tests/load/locustfile_serving.py line 49](../../../tests/load/locustfile_serving.py#L49): `POST /recommendations` load-test call.
- [jenkins/scripts/validation_load_test.sh line 19](../../../jenkins/scripts/validation_load_test.sh#L19): headless Locust command.
- [jenkins/scripts/validation_load_test.sh line 26](../../../jenkins/scripts/validation_load_test.sh#L26): HTML report output.
- [jenkins/scripts/validation_load_test.sh line 30](../../../jenkins/scripts/validation_load_test.sh#L30): CSV-to-SLA summary script.
- [jenkins/scripts/validation_load_test.sh line 44](../../../jenkins/scripts/validation_load_test.sh#L44): SLA pass/fail condition.

### 6.2 Command used

```bash
kubectl port-forward -n api-serving svc/recsys-api-serving 8088:80

NO_PROXY=127.0.0.1,localhost \
RECSYS_LOAD_HOST=http://127.0.0.1:8088 \
RECSYS_LOAD_USERS=2 \
RECSYS_LOAD_SPAWN_RATE=1 \
RECSYS_LOAD_DURATION=20s \
UV_CACHE_DIR=.uv-cache \
bash jenkins/scripts/validation_load_test.sh
```

### 6.3 SLA result

| Metric | Result | Pass condition | Status |
| --- | ---: | --- | --- |
| Total requests | `729` | recorded | PASS |
| Failure rate | `0.00%` | `0%` | PASS |
| Throughput | `38.33 req/s` | `>= 5 req/s` | PASS |
| Average latency | see HTML report | recorded | PASS |
| p95 latency | `39.00 ms` | `< 1000 ms` | PASS |
| p99 latency | see HTML report | recorded | PASS |
| Max latency | see HTML report | recorded | PASS |

Evidence files:

- `validation-verification/locust-api.html`
- `validation-verification/locust-sla-summary.md`

### 6.4 Screenshot Proof

![Data & ML system](../../pngs/locust_test.png)
