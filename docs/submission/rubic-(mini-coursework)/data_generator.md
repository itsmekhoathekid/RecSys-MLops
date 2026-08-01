# Data Generator

The repository uses two independent data-generation paths:

- **Historical/offline generator:** creates ten related tables and simulates data
  skew, high cardinality, schema evolution, and exact duplicate rows.
- **Continuous streaming producer:** creates new relational event bundles and
  simulates bursty traffic, late arrival, and duplicate replay.

Both paths keep the same table and field contracts from
[domain.py (line 194)](../../../apps/data-platform/data-generator/src/domain.py#L194)
and [schemas.py (line 11)](../../../apps/data-platform/data-generator/src/schemas.py#L11).
The problem implementations are separated so historical problems cannot be
accidentally configured as streaming problems, or vice versa.

## Code Layout

```text
apps/data-platform/data-generator/src/
├── cli.py
├── config.py
├── domain.py
├── schemas.py
├── sink.py
├── validation.py
├── behavior.py
├── randomness.py
├── drift/
│   ├── controller.py
│   └── reporting.py
├── offline/
│   ├── historical_pipeline.py
│   ├── simulation.py
│   ├── problem_pipeline.py
│   ├── payload_hash.py
│   ├── stats.py
│   └── problems/
│       ├── skew.py
│       ├── high_cardinality.py
│       ├── schema_evolution.py
│       └── exact_duplicate.py
├── streaming/
│   ├── config.py
│   ├── event_factory.py
│   ├── problem_pipeline.py
│   ├── producer.py
│   ├── postgres.py
│   ├── metrics.py
│   ├── types.py
│   └── problems/
│       ├── burst_traffic.py
│       ├── late_arrival.py
│       └── duplicate_replay.py
├── sinks/
│   ├── minio_sink.py
│   └── postgres_sink.py
└── scripts/
    ├── generate_historical_to_minio.py
    ├── load_realtime_to_postgres.py
    ├── summarize_generation_quality.py
    └── summarize_drift_label_merge.py
```

The files in the original short tree are the main orchestration and problem
files, but they are not sufficient by themselves. `behavior.py` and
`randomness.py` support historical simulation; `payload_hash.py` and `stats.py`
support offline problem injection; `metrics.py` and `types.py` support the
continuous producer; `sinks/` and the remaining scripts are used by PostgreSQL,
MinIO, Airflow, and evidence flows.

Legacy forwarding modules and unused replay stubs were removed. The offline
package now exposes only the four assessed historical problems, while the
streaming package exposes only the three assessed online-event problems.

## Generator Configuration

Both generators read the same scenario document,
[data_generator_test.yaml (line 1)](../../../configs/local/data_generator_test.yaml#L1).
There is no separate streaming-problem YAML. The single document has this
shape:

```yaml
seed: 42
offline:
  generator:
    # history, traffic, behavior, output
  problems:
    skew: {}
    high_cardinality: {}
    schema_evolution: {}
    exact_duplicate: {}
streaming:
  generator:
    # interval, events per tick, source dimensions
  problems:
    burst_traffic: {}
    duplicate_replay: {}
    late_arrival: {}
```

The root seed is shared. `offline.generator` controls historical generation,
`offline.problems` contains exactly four offline issues,
`streaming.generator` controls the continuous loop, and `streaming.problems`
contains exactly three streaming issues.

### Shared `seed`

| Field | Meaning | Runtime use |
|---|---|---|
| `seed` | One deterministic seed shared by offline and streaming generation. The same config reproduces the same offline random choices and deterministic IDs; it also seeds streaming problem sampling. | [data_generator_test.yaml (line 1)](../../../configs/local/data_generator_test.yaml#L1), [config.py (line 231)](../../../apps/data-platform/data-generator/src/config.py#L231), [config.py (line 251)](../../../apps/data-platform/data-generator/src/config.py#L251) |

### `offline.generator`

These settings describe the clean historical dataset and where it is written.
They do not define the four assessed offline problems.

| Field | Meaning and effect | Runtime use |
|---|---|---|
| `history_start_date` | First possible business date in the historical run. | [data_generator_test.yaml (line 5)](../../../configs/local/data_generator_test.yaml#L5), [simulation.py (line 507)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L507) |
| `history_days` | Number of calendar days sampled from `history_start_date`; together they define the event-time window. | [data_generator_test.yaml (line 6)](../../../configs/local/data_generator_test.yaml#L6), [simulation.py (line 508)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L508) |
| `traffic.target_behavior_events` | Desired final `behavior_events` volume, including injected exact duplicates. The simulation first estimates a smaller clean target. | [data_generator_test.yaml (line 8)](../../../configs/local/data_generator_test.yaml#L8), [simulation.py (line 60)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L60) |
| `traffic.target_tolerance` | Permitted difference around the target event count when validating the generated run. | [data_generator_test.yaml (line 9)](../../../configs/local/data_generator_test.yaml#L9), [validation.py (line 168)](../../../apps/data-platform/data-generator/src/validation.py#L168) |
| `traffic.requests_per_session_min/max` | Inclusive range for the number of recommendation requests generated in each session. | [data_generator_test.yaml (line 10)](../../../configs/local/data_generator_test.yaml#L10), [simulation.py (line 259)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L259) |
| `traffic.impressions_per_request_min/max` | Inclusive range for the number of candidate impressions attached to each request. | [data_generator_test.yaml (line 12)](../../../configs/local/data_generator_test.yaml#L12), [simulation.py (line 296)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L296) |
| `traffic.session_gap_minutes_min/max` | Reserved session-gap bounds. They are type/range validated, but the current simulation does not consume them; changing them currently does not change generated rows. | [data_generator_test.yaml (line 14)](../../../configs/local/data_generator_test.yaml#L14), [config.py (line 27)](../../../apps/data-platform/data-generator/src/config.py#L27) |
| `session_behavior.view_after_impression_base` | Base probability that an impression becomes a view; preferences, popularity, rank, and campaign context adjust it. | [data_generator_test.yaml (line 17)](../../../configs/local/data_generator_test.yaml#L17), [behavior.py (line 32)](../../../apps/data-platform/data-generator/src/behavior.py#L32) |
| `session_behavior.cart_after_view_base` | Base probability that a view becomes a cart event; category preference, price sensitivity, VIP status, and campaign context adjust it. | [data_generator_test.yaml (line 18)](../../../configs/local/data_generator_test.yaml#L18), [behavior.py (line 44)](../../../apps/data-platform/data-generator/src/behavior.py#L44) |
| `session_behavior.purchase_after_cart_base` | Base probability that a cart becomes a purchase; price sensitivity, VIP status, campaign, and optional drift adjust it. | [data_generator_test.yaml (line 19)](../../../configs/local/data_generator_test.yaml#L19), [behavior.py (line 56)](../../../apps/data-platform/data-generator/src/behavior.py#L56) |
| `burst_windows[].start_hour/end_hour` | Historical hour interval whose session-start sampling weight is increased. This shapes offline event-time density; it is not the continuous streaming burst problem. | [data_generator_test.yaml (line 20)](../../../configs/local/data_generator_test.yaml#L20), [simulation.py (line 511)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L511) |
| `burst_windows[].traffic_weight` | Multiplier applied to the historical hour's sampling weight before the 24-hour distribution is normalized. | [data_generator_test.yaml (line 23)](../../../configs/local/data_generator_test.yaml#L23), [simulation.py (line 513)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L513) |
| `output.base_path` | Parent directory for generated runs. | [data_generator_test.yaml (line 28)](../../../configs/local/data_generator_test.yaml#L28), [historical_pipeline.py (line 55)](../../../apps/data-platform/data-generator/src/offline/historical_pipeline.py#L55) |
| `output.run_id` | Reproducible run folder/name stored in the manifest. | [data_generator_test.yaml (line 29)](../../../configs/local/data_generator_test.yaml#L29), [historical_pipeline.py (line 105)](../../../apps/data-platform/data-generator/src/offline/historical_pipeline.py#L105) |
| `output.overwrite` | Allows an existing run directory to be replaced; otherwise the sink raises `FileExistsError`. | [data_generator_test.yaml (line 30)](../../../configs/local/data_generator_test.yaml#L30), [sink.py (line 62)](../../../apps/data-platform/data-generator/src/sink.py#L62) |

#### Optional `offline.generator.drift`

The normal test config omits this block, so Pydantic uses `enabled: false`.
The drift scenario declares it explicitly at
[data_generator_drift.yaml (line 27)](../../../configs/local/data_generator_drift.yaml#L27).

| Field | Meaning and effect | Contract reference |
|---|---|---|
| `enabled` | Turns purchase-frequency drift generation and drift reports on/off. | [config.py (line 87)](../../../apps/data-platform/data-generator/src/config.py#L87) |
| `scenario` | Drift type; currently only `user_purchase_frequency` is accepted. | [config.py (line 89)](../../../apps/data-platform/data-generator/src/config.py#L89) |
| `drift_start_date` | First date of the post-drift period. | [data_generator_drift.yaml (line 30)](../../../configs/local/data_generator_drift.yaml#L30) |
| `drift_mode` | `gradual` ramps the factor; `abrupt` applies it immediately. | [config.py (line 91)](../../../apps/data-platform/data-generator/src/config.py#L91) |
| `purchase_probability_multiplier` | Maximum multiplier applied to cart-to-purchase probability. | [behavior.py (line 66)](../../../apps/data-platform/data-generator/src/behavior.py#L66) |
| `ramp_up_days` | Number of days used to reach the multiplier in gradual mode. | [config.py (line 93)](../../../apps/data-platform/data-generator/src/config.py#L93) |
| `baseline_start_date/end_date` | Stable comparison window used by the drift report. | [data_generator_drift.yaml (line 34)](../../../configs/local/data_generator_drift.yaml#L34) |
| `psi_alert_threshold` | PSI level at which the generated monitoring artifact marks an alert. | [config.py (line 96)](../../../apps/data-platform/data-generator/src/config.py#L96) |

### `offline.problems`

All four offline issues are grouped together at
[data_generator_test.yaml (line 32)](../../../configs/local/data_generator_test.yaml#L32).

| Field | Meaning and effect | Runtime use |
|---|---|---|
| `skew.top_city` | City treated as the hot geographic key. It must also appear in `cities`. | [data_generator_test.yaml (line 34)](../../../configs/local/data_generator_test.yaml#L34), [config.py (line 53)](../../../apps/data-platform/data-generator/src/config.py#L53) |
| `skew.top_city_ratio` | Probability assigned to the hot city; remaining probability is split across the other cities. | [data_generator_test.yaml (line 35)](../../../configs/local/data_generator_test.yaml#L35), [skew.py (line 24)](../../../apps/data-platform/data-generator/src/offline/problems/skew.py#L24) |
| `skew.cities` | Allowed city values used when generating users. | [data_generator_test.yaml (line 36)](../../../configs/local/data_generator_test.yaml#L36), [simulation.py (line 115)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L115) |
| `skew.top_category_ratio` | Probability that category `1` becomes the selected hot category. | [data_generator_test.yaml (line 45)](../../../configs/local/data_generator_test.yaml#L45), [skew.py (line 36)](../../../apps/data-platform/data-generator/src/offline/problems/skew.py#L36) |
| `high_cardinality.n_users` | Number of user rows and user keys. | [data_generator_test.yaml (line 47)](../../../configs/local/data_generator_test.yaml#L47), [simulation.py (line 114)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L114) |
| `high_cardinality.n_products` | Number of product and current product-snapshot rows. | [data_generator_test.yaml (line 48)](../../../configs/local/data_generator_test.yaml#L48), [simulation.py (line 181)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L181) |
| `high_cardinality.n_categories` | Category key-space size; cannot exceed product count. | [data_generator_test.yaml (line 49)](../../../configs/local/data_generator_test.yaml#L49), [config.py (line 197)](../../../apps/data-platform/data-generator/src/config.py#L197) |
| `high_cardinality.n_brands` | Brand key-space size; cannot exceed product count. | [data_generator_test.yaml (line 50)](../../../configs/local/data_generator_test.yaml#L50), [config.py (line 199)](../../../apps/data-platform/data-generator/src/config.py#L199) |
| `high_cardinality.preferences_per_user` | Number of category/brand preference rows generated per user. | [data_generator_test.yaml (line 51)](../../../configs/local/data_generator_test.yaml#L51), [simulation.py (line 153)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L153) |
| `schema_evolution.change_date` | Cutover: events before it become V1 with evolved fields null; events from it onward become V2. | [data_generator_test.yaml (line 53)](../../../configs/local/data_generator_test.yaml#L53), [schema_evolution.py (line 27)](../../../apps/data-platform/data-generator/src/offline/problems/schema_evolution.py#L27) |
| `schema_evolution.breaking_change_date` | Optional later cutover for deliberately unsupported breaking rows. It must be after `change_date` and inside the history window. | [config.py (line 74)](../../../apps/data-platform/data-generator/src/config.py#L74), [config.py (line 190)](../../../apps/data-platform/data-generator/src/config.py#L190) |
| `schema_evolution.breaking_schema_version` | Version number emitted after the optional breaking cutover; minimum value is `3`. | [config.py (line 75)](../../../apps/data-platform/data-generator/src/config.py#L75), [schema_evolution.py (line 22)](../../../apps/data-platform/data-generator/src/offline/problems/schema_evolution.py#L22) |
| `exact_duplicate.rate` | Probability that each normalized historical behavior event is appended again unchanged. | [data_generator_test.yaml (line 55)](../../../configs/local/data_generator_test.yaml#L55), [exact_duplicate.py (line 13)](../../../apps/data-platform/data-generator/src/offline/problems/exact_duplicate.py#L13) |

### `streaming.generator`

These fields control the continuous producer loop at
[data_generator_test.yaml (line 57)](../../../configs/local/data_generator_test.yaml#L57).

| Field | Meaning and effect | Runtime use |
|---|---|---|
| `interval_seconds` | Sleep duration between producer ticks. | [data_generator_test.yaml (line 59)](../../../configs/local/data_generator_test.yaml#L59), [producer.py (line 91)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L91) |
| `events_per_tick` | Number of bundles emitted on a normal tick; burst ticks multiply this value. | [data_generator_test.yaml (line 60)](../../../configs/local/data_generator_test.yaml#L60), [producer.py (line 38)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L38) |
| `max_events` | Total emission limit. `0` means run continuously without an event-count limit. | [data_generator_test.yaml (line 61)](../../../configs/local/data_generator_test.yaml#L61), [producer.py (line 35)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L35) |
| `n_users` | Number of reusable streaming source users bootstrapped in PostgreSQL. | [data_generator_test.yaml (line 62)](../../../configs/local/data_generator_test.yaml#L62), [postgres.py (line 43)](../../../apps/data-platform/data-generator/src/streaming/postgres.py#L43) |
| `n_products` | Number of reusable streaming source products; the event factory cycles through this key space. | [data_generator_test.yaml (line 63)](../../../configs/local/data_generator_test.yaml#L63), [event_factory.py (line 20)](../../../apps/data-platform/data-generator/src/streaming/event_factory.py#L20) |

### `streaming.problems`

Exactly three continuous-stream issues are configured at
[data_generator_test.yaml (line 64)](../../../configs/local/data_generator_test.yaml#L64).

| Field | Meaning and effect | Runtime use |
|---|---|---|
| `burst_traffic.every_n_ticks` | Makes every Nth tick a burst tick; `0` disables scheduled bursts. | [data_generator_test.yaml (line 66)](../../../configs/local/data_generator_test.yaml#L66), [burst_traffic.py (line 6)](../../../apps/data-platform/data-generator/src/streaming/problems/burst_traffic.py#L6) |
| `burst_traffic.multiplier` | Multiplies normal `events_per_tick` on a burst tick. | [data_generator_test.yaml (line 67)](../../../configs/local/data_generator_test.yaml#L67), [burst_traffic.py (line 7)](../../../apps/data-platform/data-generator/src/streaming/problems/burst_traffic.py#L7) |
| `duplicate_replay.rate` | Probability that an event slot replays a previous bundle instead of creating a new one. | [data_generator_test.yaml (line 69)](../../../configs/local/data_generator_test.yaml#L69), [duplicate_replay.py (line 21)](../../../apps/data-platform/data-generator/src/streaming/problems/duplicate_replay.py#L21) |
| `duplicate_replay.history_size` | Maximum number of recent clean bundles retained as replay candidates. | [data_generator_test.yaml (line 70)](../../../configs/local/data_generator_test.yaml#L70), [duplicate_replay.py (line 17)](../../../apps/data-platform/data-generator/src/streaming/problems/duplicate_replay.py#L17) |
| `late_arrival.rate` | Probability that a newly created event receives a backdated business event time. | [data_generator_test.yaml (line 72)](../../../configs/local/data_generator_test.yaml#L72), [late_arrival.py (line 14)](../../../apps/data-platform/data-generator/src/streaming/problems/late_arrival.py#L14) |
| `late_arrival.delay_minutes_min/max` | Inclusive random delay interval subtracted from current time for selected late events. | [data_generator_test.yaml (line 73)](../../../configs/local/data_generator_test.yaml#L73), [late_arrival.py (line 17)](../../../apps/data-platform/data-generator/src/streaming/problems/late_arrival.py#L17) |

Pydantic loads and validates this YAML before generation through
[config.py (line 219)](../../../apps/data-platform/data-generator/src/config.py#L219).
An invalid rate, date range, or entity count therefore fails before any table
is written.

The unified document contract is defined at
[config.py (line 160)](../../../apps/data-platform/data-generator/src/config.py#L160),
the historical loader maps `offline` into the runtime model at
[config.py (line 226)](../../../apps/data-platform/data-generator/src/config.py#L226),
and the producer maps `streaming` at
[config.py (line 248)](../../../apps/data-platform/data-generator/src/config.py#L248).
Unknown streaming fields are rejected by
[config.py (line 44)](../../../apps/data-platform/data-generator/src/streaming/config.py#L44).
Helm only selects the scenario file through `DATA_GENERATOR_CONFIG`; it does not
own the problem rates.

### Previously captured generator configuration evidence

The following repository image is the configuration/drift capture referenced by
the earlier detailed version of this document:

![Generator configuration and drift evidence](../../pngs/data_gen_config_drift.png)

## Historical Data Generator

### End-to-end historical generation flow

```mermaid
flowchart TD
    CFG["Unified scenario YAML<br/>offline.generator + offline.problems"]
    LOAD["load_config()<br/>parse and validate YAML"]
    PIPE["HistoricalDataPipeline.run()"]
    INIT["RecsysSimulation<br/>seeded RNG + problem classes"]

    subgraph BASE["Generate relational data in dependency order"]
        U["1. users"]
        UP["2. user_preferences"]
        P["3. products"]
        PS["4. product_snapshots"]
        S["5. sessions"]
        R["6. recommendation_requests"]
        I["7. impressions"]
        E["8. clean behavior_events"]
        O["9. orders"]
        OI["10. order_items"]

        U --> UP
        U --> S
        P --> PS
        S --> R
        R --> I
        I --> E
        E -->|"purchase only"| O
        O --> OI
    end

    subgraph GENERATED["Problems produced while base tables are generated"]
        CITY["Data skew: city weights"]
        CAT["Data skew: hot category"]
        POP["Data skew: Pareto product popularity"]
        EXP["Data skew: preference-aware exposure"]
        CARD["High cardinality:<br/>new deterministic UUID per entity occurrence"]
    end

    subgraph INJECTED["Problems applied to clean behavior_events"]
        SE["Schema evolution<br/>V1: evolved fields null<br/>V2: evolved fields present"]
        HASH["Recompute canonical payload hash"]
        DUP["Exact duplicates<br/>same event_id and same payload"]
        SORT["Sort by ingestion_ts and event_id"]
    end

    VALIDATE["InvariantValidator<br/>PK/FK, timestamps, schema, duplicate rates"]
    WRITE["LocalParquetSink<br/>write ten tables"]
    DQ["data_quality_report.json"]
    MANIFEST["manifest.json"]
    SUMMARY["summarize_generation_quality.py<br/>volume, skew, cardinality,<br/>schema versions, duplicate rate"]

    CFG --> LOAD --> PIPE --> INIT
    INIT --> U
    INIT --> P
    CITY --> U
    CAT --> U
    CAT --> P
    POP --> P
    U --> EXP
    P --> EXP
    EXP --> R
    CARD --> S
    CARD --> R
    CARD --> I
    CARD --> E
    CARD --> O
    CARD --> OI
    E --> SE --> HASH --> DUP --> SORT
    UP --> VALIDATE
    PS --> VALIDATE
    SORT --> VALIDATE
    OI --> VALIDATE
    VALIDATE --> WRITE
    WRITE --> DQ
    WRITE --> MANIFEST
    DQ --> SUMMARY
    MANIFEST --> SUMMARY
```

The flow has two deliberately different problem stages:

1. `DataSkewProblem` and `HighCardinalityProblem` participate in creating the
   relational rows, because their effects must appear across parent and child
   tables.
2. `SchemaEvolutionProblem` and `ExactDuplicateProblem` transform the completed
   clean event list, because they are event-history issues rather than entity
   generation rules.

### Step 1: Parse and validate YAML

The CLI exposes the generation and validation commands at
[cli.py (line 13)](../../../apps/data-platform/data-generator/src/cli.py#L13).
It loads the selected YAML before constructing the pipeline at
[cli.py (line 30)](../../../apps/data-platform/data-generator/src/cli.py#L30).

Why this step matters:

- a seed makes the same configuration reproducible;
- typed ranges prevent impossible rates and negative volumes;
- the output run ID keeps evidence from different experiments separate;
- the config is later embedded in `manifest.json`, so the dataset remains
  traceable to its generation parameters.

### Step 2: Initialize deterministic simulation state

`HistoricalDataPipeline.run()` creates `RecsysSimulation` at
[historical_pipeline.py (line 25)](../../../apps/data-platform/data-generator/src/offline/historical_pipeline.py#L25).
The simulation initializes one NumPy random generator, one high-cardinality ID
allocator, and one skew implementation at
[simulation.py (line 37)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L37).

All random choices use the seeded generator. IDs are also deterministic, which
means repeated runs with the same configuration create the same logical data
while still producing many distinct IDs.

### Step 3: Generate `users` and `user_preferences`

`generate()` starts with the two user-side tables at
[simulation.py (line 49)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L49).
The user generator selects cities using skewed weights at
[simulation.py (line 111)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L111).
It also assigns preferred categories and brands, then emits multiple weighted
preferences per user at
[simulation.py (line 152)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L152).

Table relationship:

```mermaid
flowchart LR
    U["users<br/>PK: user_id"] -->|"one-to-many"| UP["user_preferences<br/>FK: user_id"]
    UP -->|"category preference later affects ranking exposure"| C["candidate selection"]
```

### Step 4: Generate `products` and `product_snapshots`

Products are created at
[simulation.py (line 174)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L174).
Each product receives a category sampled from the configured category skew at
[simulation.py (line 181)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L181)
and a long-tailed popularity weight at
[simulation.py (line 197)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L197).

The matching `product_snapshots` row records the effective catalog attributes.
This preserves the source-system relation needed by later point-in-time product
features instead of generating a disconnected event-only dataset.

### Step 5: Compute the clean event target

The requested `target_behavior_events` includes injected duplicates. The
simulation therefore divides the requested target by `1 + duplicate_rate` at
[simulation.py (line 60)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L60)
to estimate how many clean events should be produced first.

For example, a target near 10,000 rows and a 1.5% duplicate rate first produces
approximately 9,852 clean rows. The exact final count can differ slightly
because duplicate selection is probabilistic.

### Step 6: Generate session and transaction tables

For each sampled active user and historical date, `_generate_session()` creates
the dependent rows at
[simulation.py (line 242)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L242).

The dependency order inside a session is:

1. Allocate `session_id` at
   [simulation.py (line 252)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L252).
2. Allocate `request_id` and create `recommendation_requests` at
   [simulation.py (line 275)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L275).
3. Select candidate products using popularity and user preferences at
   [simulation.py (line 302)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L302).
4. Allocate `impression_id` and create ranked `impressions` at
   [simulation.py (line 306)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L306).
5. Run the behavior state machine and create view/cart/purchase events at
   [simulation.py (line 341)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L341).
6. For purchase states, allocate `order_id`, then create `orders` and
   `order_items` at
   [simulation.py (line 353)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L353).
7. Finish the parent session with start/end timestamps and its terminal reason
   at [simulation.py (line 394)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L394).

This construction order keeps foreign keys valid by design: no request exists
without a session, no impression without a request, and no order item without
an order.

### Step 7: Apply schema evolution and exact duplicates

After clean generation finishes, `HistoricalDataPipeline` creates
`OfflineProblemPipeline` at
[historical_pipeline.py (line 29)](../../../apps/data-platform/data-generator/src/offline/historical_pipeline.py#L29).
Only `behavior_events` are passed to it at
[historical_pipeline.py (line 39)](../../../apps/data-platform/data-generator/src/offline/historical_pipeline.py#L39).

The application order is explicit at
[problem_pipeline.py (line 32)](../../../apps/data-platform/data-generator/src/offline/problem_pipeline.py#L32):

```mermaid
flowchart LR
    CLEAN["Clean behavior events"] --> SCHEMA["1. Apply schema version"]
    SCHEMA --> HASH["2. Recompute payload hash"]
    HASH --> DUP["3. Sample exact duplicates"]
    DUP --> MERGE["4. Append duplicate rows"]
    MERGE --> SORT["5. Sort by ingestion time and event ID"]
```

Hashing occurs before duplication. Consequently, an exact duplicate preserves
both the same `event_id` and the same canonical payload hash; it is not merely
a new event with similar fields.

### Step 8: Validate relational and data-quality invariants

The completed ten-table bundle is validated before persistence at
[historical_pipeline.py (line 44)](../../../apps/data-platform/data-generator/src/offline/historical_pipeline.py#L44).
Generation fails rather than publishing invalid data when the validator reports
an error at
[historical_pipeline.py (line 49)](../../../apps/data-platform/data-generator/src/offline/historical_pipeline.py#L49).

The validator checks relational keys, timestamps, event payloads, schema
versions, expected volumes, and duplicate behavior through
[validation.py (line 36)](../../../apps/data-platform/data-generator/src/validation.py#L36).
This is important because a simulated problem must remain intentional: for
example, duplicate event IDs are allowed at the configured rate, but broken
foreign keys are not.

### Step 9: Write all tables in a stable order

The ten-table data contract is collected by `GeneratedData` at
[domain.py (line 194)](../../../apps/data-platform/data-generator/src/domain.py#L194).
`table_records()` fixes the write order at
[domain.py (line 206)](../../../apps/data-platform/data-generator/src/domain.py#L206):

| Order | Table | Primary dependency |
|---:|---|---|
| 1 | `users` | none |
| 2 | `user_preferences` | `users` |
| 3 | `products` | none |
| 4 | `product_snapshots` | `products` |
| 5 | `sessions` | `users` |
| 6 | `recommendation_requests` | `users`, `sessions` |
| 7 | `impressions` | `requests`, `sessions`, `products` |
| 8 | `behavior_events` | `impressions`, `requests`, `products` |
| 9 | `orders` | `users`, `sessions` |
| 10 | `order_items` | `orders`, `products` |

The pipeline iterates this mapping and writes each table through
[historical_pipeline.py (line 55)](../../../apps/data-platform/data-generator/src/offline/historical_pipeline.py#L55).
`LocalParquetSink` applies the Arrow schema and partitioning rules at
[sink.py (line 70)](../../../apps/data-platform/data-generator/src/sink.py#L70).

### Step 10: Write reproducibility and quality evidence

The pipeline computes observed duplicate, city-skew, category-skew, and schema
metrics at
[historical_pipeline.py (line 73)](../../../apps/data-platform/data-generator/src/offline/historical_pipeline.py#L73).
It writes:

- `data_quality_report.json`: validation result, injected counts, and observed
  problem metrics;
- `manifest.json`: seed, configuration hash, full configuration, row counts,
  schema versions, and table paths.

The two JSON artifacts are persisted at
[historical_pipeline.py (line 119)](../../../apps/data-platform/data-generator/src/offline/historical_pipeline.py#L119).

## Offline Data Problems

### Problem 1: Data skew

Data skew is not one random field. It is represented in four connected parts
of the relational generation flow.

#### City skew

The top city receives `top_city_ratio`; the remaining probability is split
equally across the other configured cities at
[skew.py (line 24)](../../../apps/data-platform/data-generator/src/offline/problems/skew.py#L24).
The user generator samples from those weights at
[simulation.py (line 115)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L115).

**Effect:** one geographic key becomes much more frequent in `users`, and the
same city later propagates into order shipping data. Grouping or partitioning
by city therefore encounters uneven group sizes.

#### Category skew

`category_id=1` is selected with `top_category_ratio`; other categories share
the remaining samples at
[skew.py (line 36)](../../../apps/data-platform/data-generator/src/offline/problems/skew.py#L36).
This rule is used for users at
[simulation.py (line 121)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L121)
and products at
[simulation.py (line 182)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L182).

**Effect:** the hot category appears disproportionately in the catalog and user
preferences, then propagates into impressions and behavior events.

#### Product popularity skew

Popularity weights use a Pareto distribution at
[skew.py (line 43)](../../../apps/data-platform/data-generator/src/offline/problems/skew.py#L43).
Each product stores that weight at
[simulation.py (line 197)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L197).

**Effect:** most products have modest exposure while a small head receives much
larger weights, approximating the long-tail shape of recommender traffic.

#### Preference-aware exposure skew

Candidate selection multiplies product popularity by category and brand
preference boosts at
[skew.py (line 46)](../../../apps/data-platform/data-generator/src/offline/problems/skew.py#L46).
The weighted sampling is called for every recommendation request at
[simulation.py (line 302)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L302).

**Effect:** skew is carried into the actual impressions and events instead of
existing only as an unused `popularity_weight` column.

#### How skew is proven

The evidence script calculates top city, event category, and product category
percentages at
[summarize_generation_quality.py (line 231)](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L231).
These observed ratios make the skew visible without reading raw Parquet rows.

#### Image evidence: volume, storage, and skew

![Generated table volume, Parquet storage, and skew distribution](../../pngs/data_volume_skew_dis.png)

### Problem 2: High cardinality

`HighCardinalityProblem` wraps the deterministic ID allocator at
[high_cardinality.py (line 6)](../../../apps/data-platform/data-generator/src/offline/problems/high_cardinality.py#L6).
Every call to `next_id()` returns the next distinct UUID for an entity type at
[high_cardinality.py (line 12)](../../../apps/data-platform/data-generator/src/offline/problems/high_cardinality.py#L12).

It is used for:

| Entity | Code reference | Result |
|---|---|---|
| session | [simulation.py (line 252)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L252) | distinct session key |
| recommendation request | [simulation.py (line 275)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L275) | distinct request key |
| impression | [simulation.py (line 306)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L306) | distinct exposure key |
| order | [simulation.py (line 353)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L353) | distinct purchase key |
| behavior event | [simulation.py (line 428)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L428) | distinct clean-event key |
| order item | [simulation.py (line 474)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L474) | distinct line-item key |

**How it applies:** increasing the configured entity and event counts increases
the number of unique grouping/join keys. The seed keeps those keys reproducible;
it does not reduce their cardinality.

Exact duplicates intentionally reduce the final `event_id` distinct ratio
slightly because duplicated rows preserve their original ID. This interaction
is expected and is part of the proof rather than a generator bug.

The evidence script reports rows, distinct IDs, and distinct ratio for each
major table at
[summarize_generation_quality.py (line 243)](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L243).
The column is labelled `approx_count_distinct` for rubric presentation, but the
small local proof uses an exact Python set at
[summarize_generation_quality.py (line 247)](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L247).

Downstream, the production DP3 user-aggregate job avoids materializing every
category ID in each seven-day window. It defines the `0.05` relative-standard-
deviation contract at
[build_user_aggregate_features.py (line 6)](../../../apps/data-platform/src/features/spark/build_user_aggregate_features.py#L6)
and applies `approx_count_distinct(category_id, 0.05)` at
[build_user_aggregate_features.py (line 36)](../../../apps/data-platform/src/features/spark/build_user_aggregate_features.py#L36).

### Problem 3: Schema evolution

The historical config declares the cutover date at
[data_generator_test.yaml (line 52)](../../../configs/local/data_generator_test.yaml#L52).
`SchemaEvolutionProblem.apply()` compares each event's business date with that
cutover at
[schema_evolution.py (line 20)](../../../apps/data-platform/data-generator/src/offline/problems/schema_evolution.py#L20).

| Historical period | Stored representation | Code reference |
|---|---|---|
| before cutover | V1; `device_type` and `campaign_id` are null | [schema_evolution.py (line 27)](../../../apps/data-platform/data-generator/src/offline/problems/schema_evolution.py#L27) |
| from cutover | V2; evolved columns retain generated values | [schema_evolution.py (line 29)](../../../apps/data-platform/data-generator/src/offline/problems/schema_evolution.py#L29) |
| optional breaking cutover | configured breaking schema version | [schema_evolution.py (line 22)](../../../apps/data-platform/data-generator/src/offline/problems/schema_evolution.py#L22) |

The problem pipeline applies this rule to every clean behavior event before
hashing at
[problem_pipeline.py (line 38)](../../../apps/data-platform/data-generator/src/offline/problem_pipeline.py#L38).

**How it applies:** old and new historical partitions have compatible but
different field population. Downstream Spark processing must normalize V1/V2
rows before feature computation, while an explicitly configured breaking
version can be rejected or quarantined.

The evidence script separates old, new, and optional breaking partitions and
reports version/null counts at
[summarize_generation_quality.py (line 262)](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L262).

### Problem 4: Exact duplicates

The configured historical duplicate probability is declared at
[data_generator_test.yaml (line 54)](../../../configs/local/data_generator_test.yaml#L54).
`ExactDuplicateProblem.apply()` samples completed events at
[exact_duplicate.py (line 13)](../../../apps/data-platform/data-generator/src/offline/problems/exact_duplicate.py#L13).

The class returns references to the already-normalized events; it does not
create new IDs or mutate timestamps. The pipeline appends those selected rows
at
[problem_pipeline.py (line 43)](../../../apps/data-platform/data-generator/src/offline/problem_pipeline.py#L43).

Therefore an injected exact duplicate has:

- the same `event_id`;
- the same business payload;
- the same `payload_hash`;
- the same event, creation, and ingestion timestamps.

**How it applies:** the output reproduces an at-least-once historical ingestion
issue. A downstream deduplication keyed by `event_id` can remove the additional
rows deterministically.

Production DP2 applies that native event-ID deduplication at
[build_silver_tables.py (line 45)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L45)
and returns the clean rows without a global post-deduplication sort at
[build_silver_tables.py (line 46)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L46).

The evidence script calculates duplicate rows before and after deduplication at
[summarize_generation_quality.py (line 320)](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L320)
and separately confirms exact `(event_id, payload_hash)` duplicates at
[summarize_generation_quality.py (line 123)](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L123).

#### Image evidence: cardinality, schema evolution, and deduplication

![High cardinality, schema evolution, and duplicate-rate evidence](../../pngs/cardinity_schema_dedup.png)

This image is retained from the earlier document version. Its cardinality,
schema V1/V2, and exact-duplicate rows remain relevant. The old
`conflicting duplicate` row shown in the capture is legacy evidence and is not
one of the four current offline problem classes.

## Continuous Streaming Generator

### Continuous stream event flow

```mermaid
flowchart TD
    CFG["Unified scenario YAML<br/>streaming.generator + exactly three problems"]
    LOAD["load_stream_config()<br/>strict Pydantic validation"]
    RUN["producer.run()"]
    BOOT["Bootstrap user/product dimensions"]
    TICK["Start producer tick"]
    BURST["BurstyTrafficProblem<br/>events_for_tick()"]
    LOOP["For each event slot"]
    REPLAY{"DuplicateReplayProblem<br/>replay a recent bundle?"}
    DUP["Duplicate replay<br/>same business IDs and payload<br/>refreshed arrival timestamps"]
    LATE{"LateArrivalProblem<br/>backdate event time?"}
    LT["Late event time<br/>now - configured delay"]
    ONTIME["On-time event time<br/>now"]
    FACTORY["StreamEventFactory.create()<br/>new clean relational bundle"]
    REMEMBER["Remember new bundle<br/>in bounded replay history"]
    PG["Write bundle in FK-safe order<br/>to PostgreSQL source tables"]
    COMMIT["Commit tick transaction"]
    METRICS["Publish event, burst,<br/>late, duplicate metrics"]
    CDC["Debezium CDC"]
    KAFKA["Kafka CDC topics"]
    FLINK["Flink event-time processing"]
    OFFLINE["PostgreSQL offline store"]
    ONLINE["Redis online store"]

    CFG --> LOAD --> RUN --> BOOT --> TICK --> BURST --> LOOP --> REPLAY
    REPLAY -->|"yes"| DUP --> PG
    REPLAY -->|"no"| LATE
    LATE -->|"yes"| LT --> FACTORY
    LATE -->|"no"| ONTIME --> FACTORY
    FACTORY --> REMEMBER --> PG
    PG --> LOOP
    LOOP -->|"tick complete"| COMMIT --> METRICS --> TICK
    PG --> CDC --> KAFKA --> FLINK
    FLINK --> OFFLINE
    FLINK --> ONLINE
```

The producer entry point loads the unified scenario config at
[producer.py (line 98)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L98).
`run()` creates the problem pipeline and clean event factory at
[producer.py (line 20)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L20).

Each new clean bundle contains related session, request, impression, behavior
event, and optional order rows. The factory constructs that bundle at
[event_factory.py (line 16)](../../../apps/data-platform/data-generator/src/streaming/event_factory.py#L16).
The bundle is written to PostgreSQL in dependency-safe order at
[postgres.py (line 30)](../../../apps/data-platform/data-generator/src/streaming/postgres.py#L30),
then the producer commits the tick at
[producer.py (line 59)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L59).

PostgreSQL is the streaming source system here. Debezium captures its changes
to Kafka; Flink consumes event-time records and updates the PostgreSQL offline
feature store and Redis online feature store. Iceberg is not part of this
continuous producer-to-feature-store runtime path.

## Streaming Data Problems

### Problem 1: Bursty traffic

The checked-in shared config defines the base rate at
[data_generator_test.yaml (line 60)](../../../configs/local/data_generator_test.yaml#L60)
and the burst cadence/multiplier at
[data_generator_test.yaml (line 65)](../../../configs/local/data_generator_test.yaml#L65).

`BurstTrafficProblem.events_for_tick()` returns the base size for normal ticks
and multiplies it every configured Nth tick at
[burst_traffic.py (line 6)](../../../apps/data-platform/data-generator/src/streaming/problems/burst_traffic.py#L6).
The producer requests this value before its inner emission loop at
[producer.py (line 38)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L38).

**How it applies:** with the checked-in values, a normal tick emits 40 bundles
and every fifth tick emits 320. The producer therefore creates periodic input
spikes without changing the data contract or embedding the behavior in Helm.

The producer records whether the current tick is bursty and publishes the
metric at
[producer.py (line 62)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L62).

### Problem 2: Late arrival

The rate and delay interval are configured at
[data_generator_test.yaml (line 71)](../../../configs/local/data_generator_test.yaml#L71).
`LateArrivalProblem.apply()` probabilistically subtracts a delay from `now` at
[late_arrival.py (line 14)](../../../apps/data-platform/data-generator/src/streaming/problems/late_arrival.py#L14).

`StreamProblemPipeline.event_time()` returns either the delayed timestamp or the
current timestamp at
[problem_pipeline.py (line 38)](../../../apps/data-platform/data-generator/src/streaming/problem_pipeline.py#L38).
The event factory stores this chosen value as `event_timestamp`, while keeping
`created_ts` and `ingestion_ts` at the current arrival time at
[event_factory.py (line 76)](../../../apps/data-platform/data-generator/src/streaming/event_factory.py#L76).

**How it applies:** the row is inserted into PostgreSQL now and reaches Kafka
now, but its business event time is 45-180 minutes old under the checked-in
config. Flink can therefore exercise watermarks, allowed lateness, and late-data
handling with a real difference between event time and arrival time.

Late arrival is applied only to newly created bundles at
[producer.py (line 50)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L50).
It is not an offline historical-generator problem.

### Problem 3: Duplicate replay

The replay rate and bounded history size are configured at
[data_generator_test.yaml (line 68)](../../../configs/local/data_generator_test.yaml#L68).
New clean bundles are retained in bounded history at
[duplicate_replay.py (line 17)](../../../apps/data-platform/data-generator/src/streaming/problems/duplicate_replay.py#L17).

`replay()` samples a previous bundle and deep-copies it at
[duplicate_replay.py (line 21)](../../../apps/data-platform/data-generator/src/streaming/problems/duplicate_replay.py#L21).
It refreshes arrival metadata while preserving business identity and payload at
[duplicate_replay.py (line 25)](../../../apps/data-platform/data-generator/src/streaming/problems/duplicate_replay.py#L25).
The producer attempts replay before generating a new event at
[producer.py (line 46)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L46).

**How it applies:** PostgreSQL and CDC receive the same logical event again,
with the same event identity, but at a later arrival time. This represents an
online retry/replay issue and allows Flink deduplication state to be tested.

This differs from the historical exact duplicate:

| Aspect | Historical exact duplicate | Streaming duplicate replay |
|---|---|---|
| Source | completed offline event list | bounded recent online history |
| Identity/payload | unchanged | unchanged |
| Arrival timestamps | unchanged | refreshed to replay time |
| Purpose | historical batch dedup proof | CDC/Flink replay dedup proof |

### Previously captured streaming issue evidence

![Previously captured streaming problem summary](../../pngs/stream_data_problems.png)

This image is preserved because it was referenced by the earlier detailed
document. It predates the offline/streaming package split: its burst, late, and
duplicate rows are useful historical evidence, but its `out-of-order` row is
legacy and is not part of the current continuous producer. The current
authoritative streaming scope is the three classes referenced above.

## Evidence and Output

### Run historical generation

```bash
PYTHONPATH=apps/data-platform/data-generator/src \
  .venv/bin/python apps/data-platform/data-generator/src/cli.py generate \
  --config configs/local/data_generator_test.yaml
```

### Validate the generated dataset

```bash
PYTHONPATH=apps/data-platform/data-generator/src \
  .venv/bin/python apps/data-platform/data-generator/src/cli.py validate \
  --config configs/local/data_generator_test.yaml
```

### Print problem evidence

```bash
PYTHONPATH=apps/data-platform/data-generator/src \
  .venv/bin/python \
  apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py \
  --config configs/local/data_generator_test.yaml
```

The summary command prints:

- configuration and table volumes;
- Parquet format, file count, compression, and partition layout;
- city and category skew percentages;
- row count and distinct ID count by table;
- V1/V2 population and evolved-field null percentages;
- duplicate rate before and after deduplication;
- the injected-versus-observed data-quality report.

The implementation starts the summary at
[summarize_generation_quality.py (line 109)](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L109).

### Run unit tests

```bash
.venv/bin/pytest -q \
  --override-ini='pythonpath=apps/data-platform/data-generator/src' \
  tests/unit/data_generator
```

### Output layout

For the checked-in proof config, the output is written under
`apps/data-platform/data-generator/src/output/test_10k_seed42/`:

```text
test_10k_seed42/
├── users/
├── user_preferences/
├── products/
├── product_snapshots/
├── sessions/
├── recommendation_requests/
├── impressions/
├── behavior_events/
├── orders/
├── order_items/
├── data_quality_report.json
└── manifest.json
```

The physical files are Parquet, using the explicit Arrow contracts from
[schemas.py (line 11)](../../../apps/data-platform/data-generator/src/schemas.py#L11).
The sink selects partition columns for time-based fact tables at
[sink.py (line 80)](../../../apps/data-platform/data-generator/src/sink.py#L80).

## Code Reference Map

| Concern | Main implementation |
|---|---|
| shared ten-table contract | [domain.py (line 194)](../../../apps/data-platform/data-generator/src/domain.py#L194) |
| Arrow schemas | [schemas.py (line 11)](../../../apps/data-platform/data-generator/src/schemas.py#L11) |
| historical orchestration | [historical_pipeline.py (line 21)](../../../apps/data-platform/data-generator/src/offline/historical_pipeline.py#L21) |
| clean relational simulation | [simulation.py (line 37)](../../../apps/data-platform/data-generator/src/offline/simulation.py#L37) |
| offline problem order | [problem_pipeline.py (line 15)](../../../apps/data-platform/data-generator/src/offline/problem_pipeline.py#L15) |
| skew | [skew.py (line 11)](../../../apps/data-platform/data-generator/src/offline/problems/skew.py#L11) |
| high cardinality | [high_cardinality.py (line 6)](../../../apps/data-platform/data-generator/src/offline/problems/high_cardinality.py#L6) |
| schema evolution | [schema_evolution.py (line 9)](../../../apps/data-platform/data-generator/src/offline/problems/schema_evolution.py#L9) |
| historical exact duplicates | [exact_duplicate.py (line 8)](../../../apps/data-platform/data-generator/src/offline/problems/exact_duplicate.py#L8) |
| streaming config contract | [config.py (line 44)](../../../apps/data-platform/data-generator/src/streaming/config.py#L44) |
| clean streaming event bundle | [event_factory.py (line 9)](../../../apps/data-platform/data-generator/src/streaming/event_factory.py#L9) |
| streaming problem order | [problem_pipeline.py (line 23)](../../../apps/data-platform/data-generator/src/streaming/problem_pipeline.py#L23) |
| continuous producer | [producer.py (line 20)](../../../apps/data-platform/data-generator/src/streaming/producer.py#L20) |
| bursty traffic | [burst_traffic.py (line 1)](../../../apps/data-platform/data-generator/src/streaming/problems/burst_traffic.py#L1) |
| late arrival | [late_arrival.py (line 7)](../../../apps/data-platform/data-generator/src/streaming/problems/late_arrival.py#L7) |
| streaming duplicate replay | [duplicate_replay.py (line 10)](../../../apps/data-platform/data-generator/src/streaming/problems/duplicate_replay.py#L10) |
| PostgreSQL stream sink | [postgres.py (line 30)](../../../apps/data-platform/data-generator/src/streaming/postgres.py#L30) |
| validation | [validation.py (line 36)](../../../apps/data-platform/data-generator/src/validation.py#L36) |
| problem evidence | [summarize_generation_quality.py (line 109)](../../../apps/data-platform/data-generator/src/scripts/summarize_generation_quality.py#L109) |
