# Data Generator Evidence

Run once before capturing evidence:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py --config configs/local/data_generator_test.yaml --lake-root data_platform/lake --target local
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/cli.py validate --config configs/local/data_generator_test.yaml
```

## Document Config Generator

- Configs: [`data_generator_test.yaml`](../../../configs/local/data_generator_test.yaml), [`data_generator_e2e_1k.yaml`](../../../configs/local/data_generator_e2e_1k.yaml), and [`data_generator_drift.yaml`](../../../configs/local/data_generator_drift.yaml).
- Schema and validation: [`config.py`](../../../apps/data-platform/data-generator/src/config.py).

| Config group | Purpose | Example key |
| --- | --- | --- |
| `seed`, `history_start_date`, `history_days` | deterministic run and history window | `seed: 42` |
| `entities` | entity volume and cardinality | `n_users`, `n_products`, `n_categories` |
| `traffic` | target event volume and session/request shape | `target_behavior_events` |
| `session_behavior` | click/cart/purchase probability controls | `purchase_after_cart_base` |
| `distribution` | skew controls | `top_city_ratio`, `top_category_ratio` |
| `challenges` | duplicates, late arrivals, out-of-order events | `duplicate_event_rate`, `late_arrival_rate` |
| `burst_windows` | streaming burst simulation | `traffic_weight` |
| `schema_evolution` | old/new schema boundary | `change_date` |
| `drift` | drift scenario and drift strength | `purchase_probability_multiplier` |
| `output` | run output path and overwrite behavior | `run_id`, `base_path` |

### How The Config Creates Data Problems

The generator has two useful proof configs:

- [configs/local/data_generator_test.yaml](../../../configs/local/data_generator_test.yaml) is the normal documentation proof config. It is small enough to run quickly for screenshots that explain generator behavior.
- [configs/local/data_generator_e2e_1k.yaml](../../../configs/local/data_generator_e2e_1k.yaml) is the local Kubernetes stress config used before the Spark UI proof. It intentionally increases data volume, skew, high cardinality, late arrivals, and duplicates so Spark/Flink data-quality problems are visible in runtime UIs.

| Config group | What it controls | Why it matters for proof |
| --- | --- | --- |
| `seed` | Fixes random generation. | Makes proof reproducible: rerunning the same config produces the same shape of users, products, sessions, and injected issues. |
| `history_start_date`, `history_days` | Defines the event-time window. | Gives old and new historical periods so schema evolution and late-arrival proof can be observed. |
| `entities` | Controls source cardinality: users, products, categories, brands, and preferences per user. | This is the main high-cardinality knob. More users/products/categories create more feature keys and larger feature tables before Spark writes the offline store. |
| `traffic` | Controls target behavior-event count and request/session shape. | More sessions, requests, and impressions create larger raw tables and longer Spark execution time. |
| `session_behavior` | Controls view/cart/purchase probabilities. | Changes label density and ranking/training sample volume. Higher conversion probability produces more orders and labels. |
| `distribution` | Controls city/category skew. | `top_city_ratio` and `top_category_ratio` create hot keys. A high `top_category_ratio` makes most events land on category `1`, which shows skew in Spark UI task metrics. |
| `challenges` | Controls exact duplicates, conflicting duplicates, late arrivals, and out-of-order ingestion. | This creates offline and streaming data-quality issues for deduplication, watermark/window handling, and rejected-row proof. |
| `burst_windows` | Multiplies traffic probability for selected hours. | Creates bursty event-time windows. This helps Flink UI and streaming quality windows show throughput/backpressure/burst symptoms. |
| `schema_evolution` | Defines the cutover date between schema v1 and schema v2. | Events before the cutover are generated as older-schema rows, so the proof can show missing/evolved fields being normalized. |
| `output` | Defines output path/run id and overwrite behavior. | Keeps generated artifacts stable and lets the Kubernetes generator overwrite lakehouse input before Spark reruns. |


### Implementation Mapping

| Focus | Code reference |
| --- | --- |
| Configuration and validation | [`config.py`](../../../apps/data-platform/data-generator/src/config.py) — Pydantic contracts for entity volume, traffic, skew, challenge rates, schema evolution, and drift. |
| Problem injection | [`challenges.py`](../../../apps/data-platform/data-generator/src/challenges.py) — schema evolution, late/out-of-order events, and exact/conflicting duplicates. |
| Historical generation and storage | [`pipeline.py`](../../../apps/data-platform/data-generator/src/pipeline.py), [`sink.py`](../../../apps/data-platform/data-generator/src/sink.py) — generate, validate, and persist a run. |
| Quality proof | [`summarize_generation_quality.py`](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py) — volume, skew, cardinality, schema, duplicate, and streaming summaries. |
| Drift/label proof | [`summarize_drift_label_merge.py`](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py) — drift health and training-label join evidence. |

### Running Command

```bash
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py --config configs/local/data_generator_test.yaml --lake-root data_platform/lake | awk '/## Generator Config/{flag=1} /^## Data Volume/{flag=0} flag'
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py --config configs/local/data_generator_drift.yaml | awk '/## Generator Configuration/{flag=1} /^## Drift Health/{flag=0} flag'
```

### Image Proof

![Generator drift/config proof](../../pngs/data_gen_config_drift.png)

## Offline Data Problems

| Problem | Focused code reference |
| --- | --- |
| Skew and high cardinality | [`data_generator_test.yaml`](../../../configs/local/data_generator_test.yaml), [`config.py`](../../../apps/data-platform/data-generator/src/config.py), [`summarize_generation_quality.py`](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py) |
| Schema evolution | [`challenges.py`](../../../apps/data-platform/data-generator/src/challenges.py) — `ChallengePipeline.apply()` handles the configured schema cutover. |
| Exact/conflicting duplicates | [`challenges.py`](../../../apps/data-platform/data-generator/src/challenges.py) — `ChallengePipeline.apply()` injects both duplicate modes. |
| Bronze input storage | [`sink.py`](../../../apps/data-platform/data-generator/src/sink.py), [`generate_historical_to_minio.py`](../../../apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py) |

### Running Command

```bash
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py --config configs/local/data_generator_test.yaml --lake-root data_platform/lake | awk '/## Data Volume And Storage/{flag=1} /^## Streaming Problems/{flag=0} flag'
```

### Image Proof

#### Data volume and Skew Distribution

![Data & ML system](../../pngs/data_volume_skew_dis.png)

#### High cardinity & Schema evolution & Duplicate rate

![Data & ML system](../../pngs/cardinity_schema_dedup.png)

## Online Data Problems

| Problem | Focused code reference |
| --- | --- |
| Burst windows | [`data_generator_test.yaml`](../../../configs/local/data_generator_test.yaml), [`config.py`](../../../apps/data-platform/data-generator/src/config.py) |
| Late and out-of-order events | [`challenges.py`](../../../apps/data-platform/data-generator/src/challenges.py) |
| Streaming duplicates | [`challenges.py`](../../../apps/data-platform/data-generator/src/challenges.py) — duplicate branch in `ChallengePipeline.apply()`. |
| Observed rates | [`summarize_generation_quality.py`](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py) |

### Running Command

```bash
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py --config configs/local/data_generator_test.yaml --lake-root data_platform/lake | awk '/## Streaming Problems/{flag=1} /^## Injected Vs Observed/{flag=0} flag'
```

### Image Proof

![Data & ML system](../../pngs/stream_data_problems.png)

## Data Drift And Label Merge

### Generate Data Drift

Code reference: [`data_generator_drift.yaml`](../../../configs/local/data_generator_drift.yaml), [`controller.py`](../../../apps/data-platform/data-generator/src/drift/controller.py), and [`reporting.py`](../../../apps/data-platform/data-generator/src/drift/reporting.py).

### Create Label Table And Merge With Features

Code reference: [`summarize_drift_label_merge.py`](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py) generates labels, merges features, and reports join health.

### Running Command

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/cli.py generate --config configs/local/data_generator_drift.yaml
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py --config configs/local/data_generator_drift.yaml
```

### Image Proof

![Generator drift/config proof](../../pngs/data_gen_config_drift.png)
