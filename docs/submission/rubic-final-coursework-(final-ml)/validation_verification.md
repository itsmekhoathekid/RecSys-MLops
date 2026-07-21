# Validation & Verification

## 1. Unit Test Coverage > 90%

### 1.1 Goal

- Requirement: unit tests must pass with line coverage `> 90%`.
- Scope: full `api-serving` unit test suite over `apps/api-serving/src`, including the two API service entrypoints:
  - `apps/api-serving/src/feature_api.py`: `100%`
  - `apps/api-serving/src/inference_api.py`: `100%`
- Current result: `33 passed`, `3 warnings`, total line coverage `91%`.
- Screenshot command prints the full terminal coverage table for capture.

Source references:

- [feature_api.py (line 13)](../../../apps/api-serving/src/feature_api.py#L13), [feature_api.py (line 77)](../../../apps/api-serving/src/feature_api.py#L77): online-feature singleton/warmup and health, readiness, version, metrics, POST, and GET routes.
- [inference_api.py (line 18)](../../../apps/api-serving/src/inference_api.py#L18), [inference_api.py (line 123)](../../../apps/api-serving/src/inference_api.py#L123): feature client/router singletons and recommendation service routes.
- [feature_service_client.py (line 12)](../../../apps/api-serving/src/feature_service_client.py#L12), [feature_service_client.py (line 34)](../../../apps/api-serving/src/feature_service_client.py#L34): async feature-service HTTP boundary.
- [test_validation_verification.py (line 60)](../../../tests/unit/api_serving/test_validation_verification.py#L60), [test_validation_verification.py (line 339)](../../../tests/unit/api_serving/test_validation_verification.py#L339): success, dependency failure, singleton, warmup, and mocked-client tests.

### 1.2 Command used

```bash
UV_CACHE_DIR=.uv-cache PYTHONPATH=apps/api-serving/src \
uv run pytest tests/unit/api_serving \
  --cov=apps/api-serving/src \
  --cov-report=term-missing
```

### 1.3 Screenshot proof

![Unit test coverage > 90%](../../pngs/unit_test_90.png)

## 2. Web API Tests With Fixtures And Mocks

### 2.1 Goal

- Requirement: prove Web API unit tests use pytest fixtures and mocks.
- Services under test:
  - Online Feature API: `POST /online-features`, `GET /online-features/{user_id}`, `/healthz`, `/ready`, `/version`, `/metrics`.
  - Inference API: `POST /recommendations`, `/healthz`, `/ready`, `/version`, `/metrics`.
- External dependencies mocked: Redis/Feast online feature store, feature-service HTTP client, Triton/KServe ranker/router, and environment config.

Source references:

- [`Mock` and `AsyncMock` import at test_validation_verification.py line 4](../../../tests/unit/api_serving/test_validation_verification.py#L4), [mock construction at lines 63-68](../../../tests/unit/api_serving/test_validation_verification.py#L63), and [mock injection at lines 69-81](../../../tests/unit/api_serving/test_validation_verification.py#L69): standard-library mocks plus pytest fixtures for both APIs.
- [Valid-path interaction assertions at lines 111-112](../../../tests/unit/api_serving/test_validation_verification.py#L111) and [validation short-circuit assertions at lines 138-140](../../../tests/unit/api_serving/test_validation_verification.py#L138): prove when mocked dependencies must and must not be called.
- [test_split_services.py (line 15)](../../../tests/unit/api_serving/test_split_services.py#L15), [test_split_services.py (line 139)](../../../tests/unit/api_serving/test_split_services.py#L139): route-level tests for both split services.

### 2.2 How `unittest.mock` is used

The fixtures use standard-library `Mock` objects rather than only naming hand-written classes as mocks. `spec` restricts each mock to the dependency interface, `wraps`/`side_effect` preserves deterministic output, and `AsyncMock` models the awaitable feature-service call correctly:

```python
feature_service_impl = DeterministicFeatureService()
feature_service = Mock(spec=DeterministicFeatureService)
feature_service.fetch = AsyncMock(side_effect=feature_service_impl.fetch)

ranker_impl = DeterministicRanker()
ranker = Mock(spec=DeterministicRanker, wraps=ranker_impl)
ranker.model_version = ranker_impl.model_version

monkeypatch.setattr(
    inference_api,
    "feature_service_client",
    Mock(return_value=feature_service),
)
monkeypatch.setattr(inference_api, "ranker", lambda: ranker)
```

This replaces the production feature-service HTTP client and Triton ranker before `TestClient` calls the API. The online-feature fixture separately uses [`Mock(spec=DeterministicFeatureClient, wraps=...)`](../../../tests/unit/api_serving/test_validation_verification.py#L80) to replace the Redis/Feast client. Valid recommendation requests assert `fetch.assert_awaited_once()` and `score.assert_called_once()`; invalid Pydantic boundaries assert `assert_not_awaited()` and `assert_not_called()`, proving validation returns HTTP 422 before external dependencies execute.

### 2.3 Test design

| Test area | Fixture/mock used | Expected behavior | Evidence |
| --- | --- | --- | --- |
| `POST /recommendations` | `deterministic_api` injects `Mock` feature service and ranker; async/sync call assertions verify collaboration | HTTP 200 with deterministic ranked items | [fixture at line 61](../../../tests/unit/api_serving/test_validation_verification.py#L61), [request and assertions at lines 100-112](../../../tests/unit/api_serving/test_validation_verification.py#L100) |
| `GET /online-features/{user_id}` | `deterministic_feature_api` injects a wrapped `Mock` for the Feast/Redis feature client | HTTP 200 with deterministic online feature payload | [fixture at line 78](../../../tests/unit/api_serving/test_validation_verification.py#L78), [API assertions at lines 143-181](../../../tests/unit/api_serving/test_validation_verification.py#L143) |
| Validation short circuit | The same mocks are retained on `app.state` so the test can assert neither feature retrieval nor ranking occurs for rejected input | HTTP 422 and zero dependency calls | [invalid-case assertions at lines 132-140](../../../tests/unit/api_serving/test_validation_verification.py#L132) |
| Feature API warmup | `WarmupFeatureClient` test double | startup warmup runs when enabled and skips when disabled | [warmup test at lines 281-296](../../../tests/unit/api_serving/test_validation_verification.py#L281) |
| API error handling | broken feature client, broken feature-service client, broken ranker | both services return HTTP 502 on dependency failure | [error-path test at lines 200-239](../../../tests/unit/api_serving/test_validation_verification.py#L200) |
| Singleton helpers | fake client/router classes injected with `monkeypatch` | lazy singletons are created once | [singleton test at lines 242-278](../../../tests/unit/api_serving/test_validation_verification.py#L242) |

### 2.4 Commands used

```bash
UV_CACHE_DIR=.uv-cache PYTHONPATH=apps/api-serving/src \
uv run pytest tests/unit/api_serving/test_validation_verification.py -q -vv

UV_CACHE_DIR=.uv-cache PYTHONPATH=apps/api-serving/src \
uv run pytest tests/unit/api_serving -q
```

Terminal summaries:

```text
16 passed, 5 warnings in 2.14s
41 passed, 5 warnings in 5.36s
```

### 2.5 Screenshot proof

![Web API fixtures and mocks](../../pngs/feature_and_mock_test.png)

## 3. Equivalence Partitioning And Boundary Value Analysis

### 3.1 Goal

- Requirement: use equivalence partitioning and boundary value analysis in parametrized test cases.
- Primary endpoint: `POST /recommendations`.
- Validation rules shared by `RecommendationRequest` and `OnlineFeaturesRequest`:
  - `user_id >= 1`
  - `1 <= top_k <= 100`
  - optional `candidate_item_ids` length `1..500`

### 3.2 Technique-to-code mapping

| Technique | How it is applied | Concrete code reference |
| --- | --- | --- |
| Input-partition definition | Pydantic defines the valid domain: `user_id >= 1`, optional candidate-list length `1..500`, and `top_k` in `1..100`. The same domain is applied to both recommendation and online-feature requests. | [`RecommendationRequest` fields at api_schemas.py lines 8-11](../../../apps/api-serving/src/api_schemas.py#L8) and [`OnlineFeaturesRequest` fields at lines 34-37](../../../apps/api-serving/src/api_schemas.py#L34). |
| Equivalence partitioning | One representative supplies a valid explicit candidate list; another omits the optional list and exercises the valid fallback-candidate path. Named IDs make both partitions visible in verbose pytest output. | [Representative payloads at lines 88-89](../../../tests/unit/api_serving/test_validation_verification.py#L88) and [partition IDs at lines 93-95](../../../tests/unit/api_serving/test_validation_verification.py#L93). |
| Valid boundary values | The valid test matrix exercises the inclusive lower boundaries (`user_id=1`, one candidate, `top_k=1`) and inclusive upper boundaries (500 candidates and `top_k=100`). | [Minimum and maximum payloads at lines 90-91](../../../tests/unit/api_serving/test_validation_verification.py#L90) and [their boundary IDs at lines 96-97](../../../tests/unit/api_serving/test_validation_verification.py#L96). |
| Invalid boundary values | A second parametrized matrix tests values immediately outside each accepted domain: `user_id=0`, `top_k=0`, `top_k=101`, zero candidates, and 501 candidates. | [Invalid payload matrix at lines 115-122](../../../tests/unit/api_serving/test_validation_verification.py#L115) and [descriptive IDs at lines 124-129](../../../tests/unit/api_serving/test_validation_verification.py#L124). |
| Result oracle | Valid representatives must return HTTP 200, the deterministic model version, and the expected recommendation count; invalid boundaries must return HTTP 422. Mock interaction evidence is documented in [Section 2.2](#22-how-unittestmock-is-used). | [Valid result assertions at lines 105-110](../../../tests/unit/api_serving/test_validation_verification.py#L105) and [invalid result assertion at lines 132-138](../../../tests/unit/api_serving/test_validation_verification.py#L132). |

### 3.3 Cases

| Partition or boundary | Example input | Expected result | Test ID | Code reference | Status |
| --- | --- | --- | --- | --- | --- |
| Valid explicit candidate list | `user_id=42`, `candidate_item_ids=[101,102,103]`, `top_k=2` | HTTP 200 and 2 items | `equivalence-valid-explicit-candidates` | [payload at line 88](../../../tests/unit/api_serving/test_validation_verification.py#L88) | PASS |
| Valid fallback candidates | `user_id=42`, no explicit candidates, `top_k=3` | HTTP 200 and 3 fallback items | `equivalence-valid-fallback-candidates` | [payload at line 89](../../../tests/unit/api_serving/test_validation_verification.py#L89) | PASS |
| Minimum `user_id`, `top_k`, and candidate length | `user_id=1`, `candidate_item_ids=[1]`, `top_k=1` | HTTP 200 and 1 item | `boundary-min-user-top-k-and-one-candidate` | [payload at line 90](../../../tests/unit/api_serving/test_validation_verification.py#L90) | PASS |
| Maximum `top_k` and candidate length | `candidate_item_ids=500 items`, `top_k=100` | HTTP 200 and 100 items | `boundary-max-top-k-and-max-candidates` | [payload at line 91](../../../tests/unit/api_serving/test_validation_verification.py#L91) | PASS |
| Invalid `user_id` immediately below minimum | `user_id=0` | HTTP 422 | `boundary-invalid-user-id-zero` | [payload at line 118](../../../tests/unit/api_serving/test_validation_verification.py#L118) | PASS |
| Invalid `top_k` immediately outside bounds | `top_k=0` or `top_k=101` | HTTP 422 | `boundary-invalid-top-k-*` | [payloads at lines 119-120](../../../tests/unit/api_serving/test_validation_verification.py#L119) | PASS |
| Invalid candidate-list length immediately outside bounds | `0` or `501` candidates | HTTP 422 | `boundary-invalid-candidates-*` | [payloads at lines 121-122](../../../tests/unit/api_serving/test_validation_verification.py#L121) | PASS |

Equivalence partitioning avoids testing every possible valid user ID or candidate list: the two `equivalence-*` cases represent the two materially different valid request paths. Boundary value analysis then concentrates on the exact inclusive limits and the first invalid value on either side. The shared deterministic fixture ensures that a failure is attributable to request partitioning, routing, or validation rather than an unavailable external dependency.

### 3.4 Command used

```bash
UV_CACHE_DIR=.uv-cache PYTHONPATH=apps/api-serving/src \
uv run pytest tests/unit/api_serving/test_validation_verification.py -q -vv
```

The verbose output shows all `equivalence-*` and `boundary-*` test IDs as `PASSED`.

### 3.5 Screenshot proof

![Equivalence partitioning and boundary value analysis](../../pngs/boundary_analysis.png)

## 4. Mutation Testing

### 4.1 Goal

- Requirement: use mutation testing to evaluate test effectiveness.
- Mutation score gate: `> 80%`.
- Mutation scope: core code called by the two API services:
  - `online_features.get_online_features`, used by `feature_api.py`.
  - `ranking.format_top_k`, used by `inference_api.py` through recommendation ranking.
- Current result: mutation score `86.67%`.

Source references:

- [pyproject.toml (line 66)](../../../pyproject.toml#L66): `mutmut` dependency.
- [validation-verification/mutation-summary.md](validation-verification/mutation-summary.md): mutation score.
- [validation-verification/mutation-results.txt](validation-verification/mutation-results.txt): full mutant list.

### 4.2 Technique-to-code mapping

| Mutation-testing technique | Where it is used | Concrete code reference |
| --- | --- | --- |
| Focused production targets | The proof mutates `format_top_k`, which sorts scores descending, truncates to `top_k`, and builds the recommendation response; and `get_online_features`, which chooses explicit or fallback candidates and loads user/item features. Both functions are on the production recommendation path. | [`format_top_k` at ranking.py lines 99-119](../../../apps/api-serving/src/ranking.py#L99), [`get_online_features` at online_features.py lines 273-289](../../../apps/api-serving/src/online_features.py#L273), and their production call sites at [ranking.py lines 164-176](../../../apps/api-serving/src/ranking.py#L164) and [lines 204-217](../../../apps/api-serving/src/ranking.py#L204). |
| Target and mutant filtering | `MUTATION_TARGETS` limits source files; `MUTATION_MUTANT_NAMES` narrows execution to `ranking.x_format_top_k*` and `online_features.x_get_online_features*`. The script also validates that targets belong to an approved source root. | [Target parsing and allowlist at validation_mutation.sh lines 19-49](../../../jenkins/scripts/validation_mutation.sh#L19) and [source-root validation at lines 63-99](../../../jenkins/scripts/validation_mutation.sh#L63). |
| Isolated mutation workspace | The script creates a temporary source workspace, links required repo directories, and generates a dedicated `mutmut` configuration so mutants do not rewrite the working tree. | [Temporary workspace at lines 101-112](../../../jenkins/scripts/validation_mutation.sh#L101) and [generated `tool.mutmut` configuration at lines 114-142](../../../jenkins/scripts/validation_mutation.sh#L114). |
| Covered-line mutation | `mutate_only_covered_lines = true` restricts mutation generation to statements exercised by the selected tests. `only_mutate` limits mutation to the requested source files. | [Generated configuration at lines 124-140](../../../jenkins/scripts/validation_mutation.sh#L124). |
| Test selection | The run selects focused unit tests for the two functions plus the property-based idempotency test, which traverses `recommend()` and therefore exercises feature retrieval, ranking, and top-k formatting together. | [`test_format_top_k_sorts_scores_descending` at test_serving.py lines 140-150](../../../tests/unit/api_serving/test_serving.py#L140), [`test_get_online_features_reads_candidates_sequence_and_items` at lines 373-393](../../../tests/unit/api_serving/test_serving.py#L373), and [the property test at test_validation_verification.py lines 359-392](../../../tests/unit/api_serving/test_validation_verification.py#L359). |
| Mutant execution and evidence export | `mutmut run` executes each selected mutation; the script exports CI statistics and the complete mutant-status list. | [Execution and export at validation_mutation.sh lines 147-157](../../../jenkins/scripts/validation_mutation.sh#L147). |
| Score and quality gate | Detected mutants are `killed + timeout + caught_by_type_check`; live mutants are `survived + suspicious + no_tests`. The script computes the score and fails unless it is strictly greater than the configured threshold. | [Score calculation, summary, and gate at lines 169-202](../../../jenkins/scripts/validation_mutation.sh#L169). |
| Per-target result | All 30 `format_top_k` mutants were killed. For `get_online_features`, 22 mutants were killed and 8 survived, yielding 52 killed out of 60 selected mutants overall. | [`get_online_features` results at mutation-results.txt lines 257-286](validation-verification/mutation-results.txt#L257) and [`format_top_k` results at lines 482-511](validation-verification/mutation-results.txt#L482). |

The selected test suite determines whether each mutant is killed; the report does not attribute an individual kill to one specific test case. The focused test references above show which tests cover each target, while `mutation-results.txt` is the authoritative per-mutant outcome.

### 4.3 Command used

```bash
MUTATION_TARGETS='apps/api-serving/src/ranking.py apps/api-serving/src/online_features.py' \
MUTATION_MUTANT_NAMES='ranking.x_format_top_k* online_features.x_get_online_features*' \
MUTATION_TEST_SELECTION='tests/unit/api_serving/test_serving.py tests/unit/api_serving/test_validation_verification.py::test_property_based_recommendation_idempotency_for_deterministic_prediction' \
UV_CACHE_DIR=.uv-cache \
bash jenkins/scripts/validation_mutation.sh
```

### 4.4 Result

| Metric | Result |
| --- | ---: |
| Mutation score | `86.67%` |
| Gate | `> 80%` |
| Killed mutants | `52` |
| Survived mutants | `8` |
| Timeout mutants | `0` |
| Suspicious mutants | `0` |
| No-test mutants | `0` |

### 4.5 Screenshot proof

![Mutation testing](../../pngs/mutation_testing.png)

## 5. Property-Based Idempotency Testing

### 5.1 Goal

- Requirement: use property-based testing to verify idempotency.
- Property: repeated deterministic recommendations for the same request return the same item order, scores, model version, and metadata.
- Generated inputs: `user_id`, `top_k`, and `candidate_item_ids`.
- Deterministic dependencies: hand-written feature-client and ranker test doubles.

Source references:

- [pyproject.toml (line 63)](../../../pyproject.toml#L63): `hypothesis` dependency.
- [test_validation_verification.py (line 359)](../../../tests/unit/api_serving/test_validation_verification.py#L359), [test_validation_verification.py (line 392)](../../../tests/unit/api_serving/test_validation_verification.py#L392): Hypothesis strategies, 60 generated examples, repeated predictions, and idempotency assertion.

### 5.2 Technique-to-code mapping

| Property-based technique | How it is applied | Concrete code reference |
| --- | --- | --- |
| Generated valid domain | `@given` generates `user_id` values from `1..20,000`, `top_k` from `1..100`, and non-empty candidate lists containing IDs from `1..20,000`. These ranges stay inside the Pydantic request contract. | [Hypothesis strategies at test_validation_verification.py lines 359-367](../../../tests/unit/api_serving/test_validation_verification.py#L359). |
| Reproducible test budget | `@settings(max_examples=60, deadline=None)` runs 60 generated examples and disables timing-based failure so slower CI workers do not change the functional result. | [Hypothesis settings at line 368](../../../tests/unit/api_serving/test_validation_verification.py#L368). |
| Production request construction | Each generated tuple is converted into the real `RecommendationRequest`, so Pydantic validation and the same request model used by the API remain in scope. | [Request construction at lines 369-378](../../../tests/unit/api_serving/test_validation_verification.py#L369) and [the production schema at api_schemas.py lines 8-11](../../../apps/api-serving/src/api_schemas.py#L8). |
| Deterministic dependency control | The test uses deterministic feature and ranking implementations so repeated output can differ only because of recommendation logic, not Redis, HTTP, Triton, or randomness in dependencies. | [Deterministic feature client and ranker at lines 19-46](../../../tests/unit/api_serving/test_validation_verification.py#L19) and [their construction in the property test at lines 379-380](../../../tests/unit/api_serving/test_validation_verification.py#L379). |
| Repeat-the-operation oracle | For every generated request, the test invokes the production `recommend()` function three times and serializes each complete response with `model_dump()`. | [Three repeated calls at lines 382-390](../../../tests/unit/api_serving/test_validation_verification.py#L382) and [`recommend()` production flow at ranking.py lines 122-176](../../../apps/api-serving/src/ranking.py#L122). |
| Idempotency assertion | Equality of the three complete dictionaries verifies stable user ID, item order, scores, model version, A/B metadata, and response shape for identical input and deterministic dependencies. | [Idempotency assertion at line 392](../../../tests/unit/api_serving/test_validation_verification.py#L392) and [response fields at api_schemas.py lines 19-24](../../../apps/api-serving/src/api_schemas.py#L19). |
| Downstream logic exercised | `recommend()` retrieves online features, builds the Triton payload, obtains deterministic scores, and calls `format_top_k`; therefore the property covers the integrated deterministic recommendation path rather than only a helper function. | [Feature retrieval at ranking.py lines 158-176](../../../apps/api-serving/src/ranking.py#L158) and [scoring/top-k formatting at lines 179-219](../../../apps/api-serving/src/ranking.py#L179). |

The property is deterministic idempotency, not persistence idempotency: it proves that repeating the same pure recommendation request produces the same response. It does not claim that Redis/PostgreSQL write retries are covered by this particular Hypothesis test.

### 5.3 Command used

```bash
UV_CACHE_DIR=.uv-cache PYTHONPATH=apps/api-serving/src \
uv run pytest \
  tests/unit/api_serving/test_validation_verification.py::test_property_based_recommendation_idempotency_for_deterministic_prediction \
  -q -vv
```

### 5.4 Result

| Field | Value |
| --- | --- |
| Library | Hypothesis |
| Number of examples | `60` |
| Result | PASS |

### 5.5 Screenshot proof

![Property-based idempotency testing](../../pngs/idempotency_testing.png)

## 6. Web API Load Testing With Locust

### 6.1 Goal

- Requirement: load test the Web API and produce an HTML report with SLA summary.
- Services tested with the same Locust file:
  - Inference API: `POST /recommendations` with `RECSYS_LOAD_TARGET=api`.
  - Online Feature API: `POST /online-features` with `RECSYS_LOAD_TARGET=feature`.
- Local proof run used deterministic mocked dependencies for Redis/Feast/Triton so the two FastAPI services could be tested without external infrastructure.
- Deployment proof uses the same command after port-forwarding the relevant Kubernetes service to `127.0.0.1:8088`.
- SLA: failure rate `0%`, throughput `>= 5 req/s`, and p95 latency `< 1000 ms`.

Source references:

- [pyproject.toml (line 28)](../../../pyproject.toml#L28), [pyproject.toml (line 30)](../../../pyproject.toml#L30): Locust and Uvicorn dependencies.
- [locustfile_serving.py (line 1)](../../../tests/load/locustfile_serving.py#L1), [locustfile_serving.py (line 128)](../../../tests/load/locustfile_serving.py#L128): target selection, task dispatch, and both API payloads.
- [validation_load_test.sh (line 1)](../../../jenkins/scripts/validation_load_test.sh#L1), [validation_load_test.sh (line 65)](../../../jenkins/scripts/validation_load_test.sh#L65): headless execution, HTML report, and SLA gate.

### 6.2 Commands used

Inference API:

```bash
NO_PROXY=127.0.0.1,localhost \
RECSYS_LOAD_HOST=http://127.0.0.1:8088 \
RECSYS_LOAD_TARGET=api \
RECSYS_LOAD_USERS=2 \
RECSYS_LOAD_SPAWN_RATE=1 \
RECSYS_LOAD_DURATION=8s \
UV_CACHE_DIR=.uv-cache \
bash jenkins/scripts/validation_load_test.sh
```

Online Feature API:

```bash
NO_PROXY=127.0.0.1,localhost \
REPORTS_DIR=reports/validation-feature \
EVIDENCE_DIR='docs/submission/rubic-final-coursework-(final-ml)/validation-verification/feature-load' \
RECSYS_LOAD_HOST=http://127.0.0.1:8088 \
RECSYS_LOAD_TARGET=feature \
RECSYS_LOAD_USERS=2 \
RECSYS_LOAD_SPAWN_RATE=1 \
RECSYS_LOAD_DURATION=8s \
UV_CACHE_DIR=.uv-cache \
bash jenkins/scripts/validation_load_test.sh
```

### 6.3 SLA results

| Service target | Requests | Failure rate | Throughput | p95 latency | Status | Evidence |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Inference API `/recommendations` | `376` | `0.00%` | `53.71 req/s` | `4.00 ms` | PASS | [locust-sla-summary.md](validation-verification/locust-sla-summary.md) |
| Online Feature API `/online-features` | `376` | `0.00%` | `53.66 req/s` | `3.00 ms` | PASS | [feature-load/locust-sla-summary.md](validation-verification/feature-load/locust-sla-summary.md) |

HTML reports:

- [locust-api.html (line 1)](validation-verification/locust-api.html#L1)
- [locust-api.html (line 1)](validation-verification/feature-load/locust-api.html#L1)

### 6.4 Screenshot proof

![Locust load testing](../../pngs/locust_html_api_serving.png)

![Locust load testing](../../pngs/locust_html_get_onl_feats.png)

## 7. Additional Related Test Runs

These commands were run to prove the related API-serving components still pass beyond the focused coverage gate.

```bash
UV_CACHE_DIR=.uv-cache PYTHONPATH=apps/api-serving/src \
uv run pytest tests/unit/api_serving -q

UV_CACHE_DIR=.uv-cache PYTHONPATH=apps/api-serving/src \
uv run pytest tests/contract/test_serving_contracts.py -q
```

Terminal summaries:

```text
32 passed, 3 warnings in 0.58s
13 passed in 0.44s
```
