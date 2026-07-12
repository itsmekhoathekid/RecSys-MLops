select
    event_id,
    cast(event_timestamp as timestamp(6)) as event_timestamp,
    cast(user_id as bigint) as user_id,
    session_id,
    request_id,
    impression_id,
    lower(event_type) as event_type,
    cast(product_id as bigint) as product_id,
    cast(category_id as bigint) as category_id,
    cast(brand_id as bigint) as brand_id,
    cast(price as decimal(18, 2)) as price,
    coalesce(cast(quantity as integer), 1) as quantity,
    device_type,
    source,
    campaign_id,
    order_id,
    analytics_synced_at
from {{ source('silver', 'clean_behavior_events') }}

