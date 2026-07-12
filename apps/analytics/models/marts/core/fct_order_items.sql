{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key='order_item_id',
    on_schema_change='sync_all_columns'
) }}

select
    order_item_id,
    order_id,
    product_id,
    user_id,
    session_id,
    order_timestamp,
    order_status,
    quantity,
    unit_price,
    discount_amount,
    line_amount,
    is_valid_purchase
from {{ ref('stg_orders') }}
{% if is_incremental() %}
where order_timestamp >= coalesce(
    (select date_add('day', -2, max(order_timestamp)) from {{ this }}),
    timestamp '1970-01-01 00:00:00'
)
{% endif %}

