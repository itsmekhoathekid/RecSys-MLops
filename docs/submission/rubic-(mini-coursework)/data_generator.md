# Data Generator Evidence

Run once before capturing evidence:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py --config configs/local/data_generator_test.yaml --lake-root data_platform/lake --target local
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/cli.py validate --config configs/local/data_generator_test.yaml
```

## Document Config Generator

- Config file:
  - [configs/local/data_generator_test.yaml line 1](../../../configs/local/data_generator_test.yaml#1)
  - [configs/local/data_generator_e2e_1k.yaml line 1](../../../configs/local/data_generator_e2e_1k.yaml#1)
  - [configs/local/data_generator_drift.yaml line 1](../../../configs/local/data_generator_drift.yaml#1)
- Config schema reference:
  - [apps/data-platform/data-generator/src/config.py line 10](../../../apps/data-platform/data-generator/src/config.py#10)
  - [apps/data-platform/data-generator/src/config.py line 18](../../../apps/data-platform/data-generator/src/config.py#18)
  - [apps/data-platform/data-generator/src/config.py line 45](../../../apps/data-platform/data-generator/src/config.py#45)
  - [apps/data-platform/data-generator/src/config.py line 58](../../../apps/data-platform/data-generator/src/config.py#58)
  - [apps/data-platform/data-generator/src/config.py line 73](../../../apps/data-platform/data-generator/src/config.py#73)
  - [apps/data-platform/data-generator/src/config.py line 83](../../../apps/data-platform/data-generator/src/config.py#83)
  - [apps/data-platform/data-generator/src/config.py line 122](../../../apps/data-platform/data-generator/src/config.py#122)

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

- [configs/local/data_generator_test.yaml line 1](../../../configs/local/data_generator_test.yaml#1) is the normal documentation proof config. It is small enough to run quickly for screenshots that explain generator behavior.
- [configs/local/data_generator_e2e_1k.yaml line 1](../../../configs/local/data_generator_e2e_1k.yaml#1) is the local Kubernetes stress config used before the Spark UI proof. It intentionally increases data volume, skew, high cardinality, late arrivals, and duplicates so Spark/Flink data-quality problems are visible in runtime UIs.

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

- [apps/data-platform/data-generator/src/config.py line 10](../../../apps/data-platform/data-generator/src/config.py#L10): validates entity cardinality (`n_users`, `n_products`, `n_categories`, `n_brands`, `preferences_per_user`).
- [apps/data-platform/data-generator/src/config.py line 18](../../../apps/data-platform/data-generator/src/config.py#L18): validates traffic volume and per-session/request ranges.
- [apps/data-platform/data-generator/src/config.py line 45](../../../apps/data-platform/data-generator/src/config.py#L45): validates skew controls (`top_city_ratio`, `top_category_ratio`).
- [apps/data-platform/data-generator/src/config.py line 58](../../../apps/data-platform/data-generator/src/config.py#L58): validates data challenge rates for duplicates, late arrivals, and out-of-order events.
- [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 254](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L254): prints observed skew distribution from generated output.
- [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 266](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L266): prints observed entity/table cardinality from generated output.
- [apps/data-platform/data-generator/src/challenges.py line 76](../../../apps/data-platform/data-generator/src/challenges.py#L76): applies schema evolution before/after the configured cutover date.
- [apps/data-platform/data-generator/src/challenges.py line 89](../../../apps/data-platform/data-generator/src/challenges.py#L89): injects late arrivals by delaying `created_ts`.
- [apps/data-platform/data-generator/src/challenges.py line 102](../../../apps/data-platform/data-generator/src/challenges.py#L102): injects out-of-order ingestion timestamps.
- [apps/data-platform/data-generator/src/challenges.py line 123](../../../apps/data-platform/data-generator/src/challenges.py#L123): injects conflicting duplicates with changed payloads.
- [apps/data-platform/data-generator/src/challenges.py line 138](../../../apps/data-platform/data-generator/src/challenges.py#L138): injects exact duplicate events.

### Running Command

```bash
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py --config configs/local/data_generator_test.yaml --lake-root data_platform/lake | awk '/## Generator Config/{flag=1} /^## Data Volume/{flag=0} flag'
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py --config configs/local/data_generator_drift.yaml | awk '/## Generator Configuration/{flag=1} /^## Drift Health/{flag=0} flag'
```

### Image Proof

![Generator drift/config proof](../../pngs/data_gen_config_drift.png)

## Offline Data Problems

### Generate Skew

- Code reference:
  - [configs/local/data_generator_test.yaml line 27](../../../configs/local/data_generator_test.yaml#27)
  - [apps/data-platform/data-generator/src/config.py line 45](../../../apps/data-platform/data-generator/src/config.py#45)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 254](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#254)

### Generate High Cardinality

- Code reference:
  - [configs/local/data_generator_test.yaml line 5](../../../configs/local/data_generator_test.yaml#5)
  - [apps/data-platform/data-generator/src/config.py line 10](../../../apps/data-platform/data-generator/src/config.py#10)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 266](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#266)

### Generate Schema Evolution

- Code reference:
  - [configs/local/data_generator_test.yaml line 57](../../../configs/local/data_generator_test.yaml#57)
  - [apps/data-platform/data-generator/src/config.py line 79](../../../apps/data-platform/data-generator/src/config.py#79)
  - [apps/data-platform/data-generator/src/challenges.py line 76](../../../apps/data-platform/data-generator/src/challenges.py#76)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 148](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#148)

### Generate Duplicate Rate

- Code reference:
  - [configs/local/data_generator_test.yaml line 42](../../../configs/local/data_generator_test.yaml#42)
  - [apps/data-platform/data-generator/src/config.py line 58](../../../apps/data-platform/data-generator/src/config.py#58)
  - [apps/data-platform/data-generator/src/challenges.py line 123](../../../apps/data-platform/data-generator/src/challenges.py#123)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 129](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#129)

### Store Data For Bronze Ingestion

- Code reference:
  - [configs/local/data_generator_test.yaml line 60](../../../configs/local/data_generator_test.yaml#60)
  - [apps/data-platform/data-generator/src/sink.py line 33](../../../apps/data-platform/data-generator/src/sink.py#33)
  - [apps/data-platform/data-generator/src/sink.py line 66](../../../apps/data-platform/data-generator/src/sink.py#66)
  - [apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py line 34](../../../apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py#34)

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

### Generate Burst

- Code reference:
  - [configs/local/data_generator_test.yaml line 49](../../../configs/local/data_generator_test.yaml#49)
  - [apps/data-platform/data-generator/src/config.py line 73](../../../apps/data-platform/data-generator/src/config.py#73)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 168](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#168)

### Generate Late Arrivals

- Code reference:
  - [configs/local/data_generator_test.yaml line 44](../../../configs/local/data_generator_test.yaml#44)
  - [apps/data-platform/data-generator/src/config.py line 61](../../../apps/data-platform/data-generator/src/config.py#61)
  - [apps/data-platform/data-generator/src/challenges.py line 89](../../../apps/data-platform/data-generator/src/challenges.py#89)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 156](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#156)

### Generate Streaming Duplicate Rate

- Code reference:
  - [configs/local/data_generator_test.yaml line 42](../../../configs/local/data_generator_test.yaml#42)
  - [apps/data-platform/data-generator/src/challenges.py line 138](../../../apps/data-platform/data-generator/src/challenges.py#138)
  - [apps/data-platform/data-generator/src/challenges.py line 144](../../../apps/data-platform/data-generator/src/challenges.py#144)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 132](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#132)

### Generate Out-Of-Order Ingestion

- Code reference:
  - [configs/local/data_generator_test.yaml line 45](../../../configs/local/data_generator_test.yaml#45)
  - [apps/data-platform/data-generator/src/config.py line 62](../../../apps/data-platform/data-generator/src/config.py#62)
  - [apps/data-platform/data-generator/src/challenges.py line 102](../../../apps/data-platform/data-generator/src/challenges.py#102)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 162](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#162)

### Running Command

```bash
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py --config configs/local/data_generator_test.yaml --lake-root data_platform/lake | awk '/## Streaming Problems/{flag=1} /^## Injected Vs Observed/{flag=0} flag'
```

### Image Proof

![Data & ML system](../../pngs/stream_data_problems.png)

## Data Drift And Label Merge

### Generate Data Drift

- Code reference:
  - [configs/local/data_generator_drift.yaml line 60](../../../configs/local/data_generator_drift.yaml#60)
  - [apps/data-platform/data-generator/src/drift/controller.py line 8](../../../apps/data-platform/data-generator/src/drift/controller.py#8)
  - [apps/data-platform/data-generator/src/drift/reporting.py line 137](../../../apps/data-platform/data-generator/src/drift/reporting.py#137)
  - [apps/data-platform/data-generator/src/drift/reporting.py line 161](../../../apps/data-platform/data-generator/src/drift/reporting.py#161)

### Create Label Table And Merge With Features

- Code reference:
  - [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 46](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#46)
  - [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 84](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#84)
  - [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 95](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#95)
  - [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 175](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#175)
  - [apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py line 183](../../../apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py#183)

### Running Command

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/cli.py generate --config configs/local/data_generator_drift.yaml
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py --config configs/local/data_generator_drift.yaml
```

### Image Proof

![Generator drift/config proof](../../pngs/data_gen_config_drift.png)
