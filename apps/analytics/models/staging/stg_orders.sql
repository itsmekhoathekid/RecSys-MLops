select
    item.order_item_id,
    item.order_id,
    cast(item.product_id as bigint) as product_id,
    cast(header.user_id as bigint) as user_id,
    header.session_id,
    cast(header.order_timestamp as timestamp(6)) as order_timestamp,
    lower(header.status) as order_status,
    cast(item.quantity as integer) as quantity,
    cast(item.unit_price as decimal(18, 2)) as unit_price,
    cast(item.discount_amount as decimal(18, 2)) as discount_amount,
    cast(item.line_amount as decimal(18, 2)) as line_amount,
    case
        when header.status is null then false
        when lower(header.status) in ('cancelled', 'refunded') then false
        else true
    end as is_valid_purchase,
    coalesce(
        greatest(item.analytics_synced_at, header.analytics_synced_at),
        item.analytics_synced_at,
        header.analytics_synced_at
    ) as analytics_synced_at
from {{ source('lakehouse', 'order_items') }} item
left join {{ source('lakehouse', 'orders') }} header
  on item.order_id = header.order_id
