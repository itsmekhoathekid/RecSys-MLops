select
    cast(product_id as bigint) as product_id,
    cast(valid_from as timestamp(6)) as valid_from,
    cast(valid_to as timestamp(6)) as valid_to,
    cast(category_id as bigint) as category_id,
    category_code,
    cast(brand_id as bigint) as brand_id,
    brand_name,
    cast(current_price as decimal(18, 2)) as current_price,
    cast(price_bucket as integer) as price_bucket,
    is_active,
    analytics_synced_at
from {{ source('silver', 'product_scd') }}
