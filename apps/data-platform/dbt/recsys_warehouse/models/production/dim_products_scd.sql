{{ config(schema='production') }}

select
    product_id::bigint as product_id,
    valid_from,
    valid_to,
    category_id::bigint as category_id,
    category_code::text as category_code,
    brand_id::bigint as brand_id,
    brand_name::text as brand_name,
    current_price::double precision as current_price,
    price_bucket::smallint as price_bucket,
    is_active::boolean as is_active,
    created_ts
from {{ source('staging', 'product_snapshots') }}

union all

select
    product_id::bigint as product_id,
    created_ts as valid_from,
    null::timestamp with time zone as valid_to,
    category_id::bigint as category_id,
    category_code::text as category_code,
    brand_id::bigint as brand_id,
    brand_name::text as brand_name,
    current_price::double precision as current_price,
    price_bucket::smallint as price_bucket,
    is_active::boolean as is_active,
    created_ts
from {{ source('staging', 'products') }}

