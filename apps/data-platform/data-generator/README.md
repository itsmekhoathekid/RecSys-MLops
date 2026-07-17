# Recsys Data Generator

Deterministic historical-data simulator for the BST recommender pipeline. It
generates clean, relationally consistent records first, then injects controlled
data-quality challenges before writing explicit-schema Parquet files.

## Run

```bash
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/cli.py generate \
  --config configs/local/data_generator_test.yaml

PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/cli.py validate \
  --config configs/local/data_generator_test.yaml
```

Output is written to `apps/data-platform/data-generator/src/output/<run_id>/`. Time-series tables use
`business_date=YYYY-MM-DD` directories. Each run also contains:

- `manifest.json`: seed, config hash, row counts, schemas, and file paths.
- `data_quality_report.json`: injected and observed challenge metrics.

`0` is reserved for PAD/UNKNOWN in downstream encoded features. Generated
business IDs start at `1`.

## Generation flow

```text
users/products
  -> sessions
  -> recommendation requests
  -> candidate impressions
  -> impression/view/cart/purchase state machine
  -> orders and order items
  -> challenge injectors
  -> validation
  -> partitioned Parquet
```

The configured target is the number of `behavior_events` after exact-duplicate
injection. Generation finishes the current session before
stopping, so the final count uses the configured tolerance.

## Unified configuration

Each scenario YAML contains both paths under one document:

```yaml
seed: 42
offline:
  generator: {}
  problems:
    skew: {}
    high_cardinality: {}
    schema_evolution: {}
    exact_duplicate: {}
streaming:
  generator: {}
  problems:
    burst_traffic: {}
    duplicate_replay: {}
    late_arrival: {}
```

The historical CLI reads `offline`; the continuous producer reads `streaming`;
both reuse the root seed.

## Purchase-frequency drift

`configs/local/data_generator_drift.yaml` enables Scenario A over 150 historical days:

The drift block lives at `offline.generator.drift`; it remains separate from
the assessed offline problem classes.

The controller multiplies only the `cart -> purchase` probability. It does not
rewrite feature values, so order and rolling-feature drift emerge from upstream
behavior naturally. Gradual mode linearly increases the factor from `1.0` to
the target multiplier; abrupt mode applies the target on the start date.

Every behavior event and order contains:

| Column | Purpose |
|---|---|
| `drift_enabled` | Whether drift was enabled for the run |
| `drift_scenario` | `user_purchase_frequency`, otherwise null |
| `drift_phase` | `disabled`, `baseline`, `pre_drift`, or `post_drift` |
| `drift_factor` | Effective multiplier at the business timestamp |

Run the drift scenario:

```bash
PYTHONPATH=apps/data-platform/data-generator/src uv run python apps/data-platform/data-generator/src/cli.py generate \
  --config configs/local/data_generator_drift.yaml
```

Drift-enabled runs add:

- `reports/user_daily_features.parquet`: zero-filled rolling 90-day features
  for every active user and calendar date.
- `reports/drift_validation_report.csv`: daily mean, standard deviation, PSI,
  status, and configured factor for each monitored feature.
- `monitoring/agg_feature_health_daily.parquet`: typed monitoring equivalent
  of the CSV report.
- `monitoring/feature_drift_alerts.parquet`: rows whose PSI reaches the
  configured alert threshold on or after `drift_start_date`.

Monitored features are `f_user_purchase_count_90d`,
`f_user_total_orders_90d`, and `f_user_interaction_count_90d`. PSI compares
each daily user distribution with the pooled configured baseline distribution.
Quantile bins are learned from the baseline and protected against all-zero or
duplicate breakpoints.

## Data challenges

| Challenge | Representation |
|---|---|
| Skew | Weighted city/category generation and preference-aware exposure |
| High cardinality | Deterministic UUIDs for sessions, requests, impressions, events, and orders |
| Schema evolution | V1 events omit `device_type` and `campaign_id`; V2 populates them |
| Exact duplicate | Repeated row with the same `event_id` and `payload_hash` |
| Bursty streaming traffic | Continuous producer periodically multiplies the tick size |
| Streaming late arrival | Continuous events arrive now with a backdated `event_timestamp` |
| Streaming duplicate replay | A recent event bundle is replayed with the same business identity |

## Table dictionary

### `users`

Grain: one current row per user. Primary key: `user_id`.

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `user_id` | BIGINT | No | User business key |
| `signup_ts` | TIMESTAMPTZ | No | Registration time |
| `signup_channel` | STRING | No | Organic, ads, or referral acquisition |
| `city` | STRING | No | User city and skew dimension |
| `country` | STRING | No | User country |
| `segment` | STRING | No | New, regular, or VIP segment |
| `age_bucket` | SMALLINT | No | Coarse age category |
| `preferred_category_id` | BIGINT | No | Main category affinity |
| `preferred_brand_id` | BIGINT | No | Main brand affinity |
| `price_sensitivity` | DOUBLE | No | Purchase-price sensitivity in `[0,1]` |
| `user_lifecycle_state` | STRING | No | Active, dormant, or churned |
| `last_active_ts` | TIMESTAMPTZ | No | Latest generated session end |
| `is_active` | BOOLEAN | No | Whether the user can be active |
| `created_ts` | TIMESTAMPTZ | No | Source creation time |
| `updated_ts` | TIMESTAMPTZ | No | Source update time |

### `user_preferences`

Grain: one category/brand preference per user. Key:
`(user_id, category_id, brand_id)`.

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `user_id` | BIGINT | No | Parent user |
| `category_id` | BIGINT | No | Preferred category |
| `brand_id` | BIGINT | Yes | Optional preferred brand |
| `preference_weight` | DOUBLE | No | Relative affinity weight |
| `source` | STRING | No | How the preference was obtained |
| `created_ts` | TIMESTAMPTZ | No | Creation time |
| `updated_ts` | TIMESTAMPTZ | No | Update time |

### `products`

Grain: one current row per product. Primary key: `product_id`.

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `product_id` | BIGINT | No | Product business key |
| `product_name` | STRING | No | Display name |
| `category_id` | BIGINT | No | Encodable category key |
| `category_code` | STRING | No | Human-readable category |
| `brand_id` | BIGINT | No | Encodable brand key |
| `brand_name` | STRING | No | Human-readable brand |
| `base_price` | DECIMAL(18,2) | No | Price before discount |
| `current_price` | DECIMAL(18,2) | No | Current selling price |
| `price_bucket` | SMALLINT | No | Price embedding bucket 1-10 |
| `popularity_weight` | DOUBLE | No | Candidate-selection weight |
| `is_active` | BOOLEAN | No | Candidate eligibility |
| `created_ts` | TIMESTAMPTZ | No | Product creation time |
| `updated_ts` | TIMESTAMPTZ | No | Last metadata update |

### `product_snapshots`

Grain: one product metadata version per validity interval. Key:
`(product_id, valid_from)`.

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `product_id` | BIGINT | No | Parent product |
| `valid_from` | TIMESTAMPTZ | No | Version effective start |
| `valid_to` | TIMESTAMPTZ | Yes | Version effective end |
| `category_id`, `category_code` | BIGINT, STRING | No | Category at that time |
| `brand_id`, `brand_name` | BIGINT, STRING | No | Brand at that time |
| `current_price` | DECIMAL(18,2) | No | Effective price |
| `price_bucket` | SMALLINT | No | Effective price bucket |
| `is_active` | BOOLEAN | No | Effective active status |
| `created_ts` | TIMESTAMPTZ | No | Snapshot creation time |

### `sessions`

Grain: one browsing session. Primary key: `session_id`.

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `session_id` | UUID string | No | Session key |
| `user_id` | BIGINT | No | Session owner |
| `session_start_ts`, `session_end_ts` | TIMESTAMPTZ | No | Session interval |
| `entry_source` | STRING | No | App or web entry source |
| `device_type` | STRING | No | Mobile, desktop, or tablet |
| `campaign_id` | STRING | Yes | Acquiring campaign |
| `session_end_reason` | STRING | No | Purchase, abandon, browse, or bounce |
| `created_ts`, `updated_ts` | TIMESTAMPTZ | No | Source audit times |

### `recommendation_requests`

Grain: one recommendation API request. Primary key: `request_id`.

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `request_id` | UUID string | No | Request key |
| `user_id`, `session_id` | BIGINT, UUID string | No | Request owner and session |
| `request_timestamp` | TIMESTAMPTZ | No | Prediction time |
| `surface` | STRING | No | Homepage, PDP, cart, or search |
| `context_product_id` | BIGINT | Yes | Optional contextual product |
| `context_category_id` | BIGINT | Yes | Optional contextual category |
| `device_type` | STRING | Yes | V2 request device |
| `source` | STRING | No | App or web |
| `campaign_id` | STRING | Yes | V2 campaign |
| `created_ts` | TIMESTAMPTZ | No | Source creation time |
| `schema_version` | SMALLINT | No | Source schema version |

### `impressions`

Grain: one exposed candidate per request. Primary key: `impression_id`.

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `impression_id` | UUID string | No | Exposure key |
| `request_id`, `user_id`, `session_id` | IDs | No | Exposure lineage |
| `impression_timestamp` | TIMESTAMPTZ | No | Product display time |
| `candidate_product_id` | BIGINT | No | Displayed product |
| `rank_position` | INTEGER | No | Display rank |
| `candidate_source` | STRING | No | Category or popularity retrieval |
| `retrieval_score` | DOUBLE | No | Retrieval-stage score |
| `ranking_score` | DOUBLE | No | Simulated ranking score |
| `surface` | STRING | No | Recommendation surface |
| `is_clicked` | BOOLEAN | No | Whether a view followed |
| `created_ts` | TIMESTAMPTZ | No | Source creation time |
| `schema_version` | SMALLINT | No | Source schema version |

### `behavior_events`

Grain: one emitted event row; duplicates intentionally violate uniqueness of
`event_id`.

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `event_id` | UUID string | No | Deduplication key |
| `event_timestamp` | TIMESTAMPTZ | No | Business event time |
| `created_ts` | TIMESTAMPTZ | No | Source creation/late-arrival time |
| `ingestion_ts` | TIMESTAMPTZ | No | Simulated stream arrival time |
| `user_id`, `session_id` | IDs | No | Event actor and session |
| `request_id`, `impression_id` | UUID string | Yes | Recommendation lineage |
| `event_type` | STRING | No | View, cart, or purchase |
| `product_id`, `category_id`, `brand_id` | BIGINT | No | Product metadata at event time |
| `price` | DECIMAL(18,2) | No | Event-time price |
| `price_bucket` | SMALLINT | No | Event-time price bucket |
| `quantity` | INTEGER | No | Event quantity |
| `device_type` | STRING | Yes | Missing in schema V1 |
| `source` | STRING | No | App or web |
| `campaign_id` | STRING | Yes | Missing in V1 or no campaign |
| `page_context` | STRING | Yes | Surface that produced the event |
| `rank_position` | INTEGER | Yes | Recommendation position |
| `order_id` | UUID string | Yes | Required for purchases |
| `payload_hash` | STRING | No | Exact/conflicting duplicate detection |
| `event_date` | DATE | No | Event-date partition/audit field |
| `schema_version` | SMALLINT | No | V1 or V2 payload |

### `orders`

Grain: one order. Primary key: `order_id`.

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `order_id` | UUID string | No | Order key |
| `user_id`, `session_id` | IDs | No | Buyer and originating session |
| `order_timestamp` | TIMESTAMPTZ | No | Purchase time |
| `status` | STRING | No | Current order status |
| `gross_amount` | DECIMAL(18,2) | No | Amount before discount |
| `discount_amount` | DECIMAL(18,2) | No | Order discount |
| `net_amount` | DECIMAL(18,2) | No | Final amount |
| `coupon_code` | STRING | Yes | Applied coupon |
| `payment_method` | STRING | Yes | COD, card, or wallet |
| `shipping_city` | STRING | Yes | Delivery city |
| `paid_ts`, `cancelled_ts`, `refunded_ts` | TIMESTAMPTZ | Yes | Status timestamps |
| `created_ts`, `updated_ts` | TIMESTAMPTZ | No | Source audit times |

### `order_items`

Grain: one product line per order. Primary key: `order_item_id`.

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `order_item_id` | UUID string | No | Line key |
| `order_id` | UUID string | No | Parent order |
| `product_id` | BIGINT | No | Purchased product |
| `quantity` | INTEGER | No | Purchased units |
| `unit_price` | DECIMAL(18,2) | No | Unit selling price |
| `discount_amount` | DECIMAL(18,2) | No | Line discount |
| `line_amount` | DECIMAL(18,2) | No | Net line amount |
| `created_ts` | TIMESTAMPTZ | No | Line creation time |
