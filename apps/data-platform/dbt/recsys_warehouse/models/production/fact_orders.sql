{{ config(schema='production') }}

select
    order_id::text as order_id,
    user_id::bigint as user_id,
    session_id::text as session_id,
    order_timestamp,
    status::text as status,
    gross_amount::double precision as gross_amount,
    discount_amount::double precision as discount_amount,
    net_amount::double precision as net_amount,
    coupon_code::text as coupon_code,
    payment_method::text as payment_method,
    shipping_city::text as shipping_city,
    paid_ts,
    cancelled_ts,
    refunded_ts,
    created_ts,
    updated_ts
from {{ source('staging', 'orders') }}

