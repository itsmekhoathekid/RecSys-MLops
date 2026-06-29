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

### Running Command

```bash
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py --config configs/local/data_generator_test.yaml --lake-root data_platform/lake | awk '/## Generator Config/{flag=1} /^## Data Volume/{flag=0} flag'
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/scripts/summarize_drift_label_merge.py --config configs/local/data_generator_drift.yaml | awk '/## Generator Configuration/{flag=1} /^## Drift Health/{flag=0} flag'
```

### Image Proof

TODO: screenshot generator configuration output.

## Offline Data Problems

### Simulate Skew

- Code reference:
  - [configs/local/data_generator_test.yaml line 27](../../../configs/local/data_generator_test.yaml#27)
  - [apps/data-platform/data-generator/src/config.py line 45](../../../apps/data-platform/data-generator/src/config.py#45)
  - [apps/data-platform/data-generator/src/simulation.py line 117](../../../apps/data-platform/data-generator/src/simulation.py#117)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 254](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#254)

### Simulate High Cardinality

- Code reference:
  - [configs/local/data_generator_test.yaml line 5](../../../configs/local/data_generator_test.yaml#5)
  - [apps/data-platform/data-generator/src/config.py line 10](../../../apps/data-platform/data-generator/src/config.py#10)
  - [apps/data-platform/data-generator/src/simulation.py line 426](../../../apps/data-platform/data-generator/src/simulation.py#426)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 266](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#266)

### Simulate Schema Evolution

- Code reference:
  - [configs/local/data_generator_test.yaml line 57](../../../configs/local/data_generator_test.yaml#57)
  - [apps/data-platform/data-generator/src/config.py line 79](../../../apps/data-platform/data-generator/src/config.py#79)
  - [apps/data-platform/data-generator/src/challenges.py line 76](../../../apps/data-platform/data-generator/src/challenges.py#76)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 148](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#148)

### Simulate Duplicate Rate

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

### Simulate Burst

- Code reference:
  - [configs/local/data_generator_test.yaml line 49](../../../configs/local/data_generator_test.yaml#49)
  - [apps/data-platform/data-generator/src/config.py line 73](../../../apps/data-platform/data-generator/src/config.py#73)
  - [apps/data-platform/data-generator/src/simulation.py line 527](../../../apps/data-platform/data-generator/src/simulation.py#527)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 168](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#168)

### Simulate Late Arrivals

- Code reference:
  - [configs/local/data_generator_test.yaml line 44](../../../configs/local/data_generator_test.yaml#44)
  - [apps/data-platform/data-generator/src/config.py line 61](../../../apps/data-platform/data-generator/src/config.py#61)
  - [apps/data-platform/data-generator/src/challenges.py line 89](../../../apps/data-platform/data-generator/src/challenges.py#89)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 156](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#156)

### Simulate Streaming Duplicate Rate

- Code reference:
  - [configs/local/data_generator_test.yaml line 42](../../../configs/local/data_generator_test.yaml#42)
  - [apps/data-platform/data-generator/src/challenges.py line 138](../../../apps/data-platform/data-generator/src/challenges.py#138)
  - [apps/data-platform/data-generator/src/challenges.py line 144](../../../apps/data-platform/data-generator/src/challenges.py#144)
  - [apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py line 132](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#132)

### Simulate Out-Of-Order Ingestion

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

### Simulate Data Drift

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

TODO: screenshot output showing generator configuration, label table `user_id,label`, and merged features with labels.
