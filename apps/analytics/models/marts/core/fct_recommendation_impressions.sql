{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['impression_id', 'product_id'],
    on_schema_change='sync_all_columns'
) }}

select
    impression_id,
    product_id,
    request_id,
    user_id,
    session_id,
    impression_timestamp,
    rank_position,
    candidate_source,
    retrieval_score,
    ranking_score,
    surface,
    experiment_id,
    variant,
    has_click,
    has_cart,
    has_purchase,
    clicked_at,
    carted_at,
    purchased_at
from {{ ref('int_impression_outcomes') }}
{% if is_incremental() %}
where impression_timestamp >= coalesce(
    (select date_add('day', -2, max(impression_timestamp)) from {{ this }}),
    timestamp '1970-01-01 00:00:00'
)
{% endif %}

