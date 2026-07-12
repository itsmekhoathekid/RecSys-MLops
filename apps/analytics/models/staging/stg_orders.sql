select
    order_item_id,
    order_id,
    cast(product_id as bigint) as product_id,
    cast(user_id as bigint) as user_id,
    session_id,
    cast(order_timestamp as timestamp(6)) as order_timestamp,
    lower(status) as order_status,
    cast(quantity as integer) as quantity,
    cast(unit_price as decimal(18, 2)) as unit_price,
    cast(discount_amount as decimal(18, 2)) as discount_amount,
    cast(line_amount as decimal(18, 2)) as line_amount,
    coalesce(is_valid_purchase, false) as is_valid_purchase,
    analytics_synced_at
from {{ source('silver', 'order_facts') }}

