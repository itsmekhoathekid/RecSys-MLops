select
    product_id,
    valid_from,
    valid_to,
    category_id,
    category_code,
    brand_id,
    brand_name,
    current_price,
    price_bucket,
    is_active,
    valid_to is null as is_current
from {{ ref('stg_products') }}

