{{ config(schema='production') }}

with historical as (
    select
        event_id::text as event_id,
        event_timestamp,
        created_ts,
        ingestion_ts,
        user_id::bigint as user_id,
        session_id::text as session_id,
        request_id::text as request_id,
        impression_id::text as impression_id,
        event_type::text as event_type,
        case event_type
            when 'view' then 1
            when 'cart' then 2
            when 'purchase' then 3
            else 0
        end::smallint as event_type_id,
        product_id::bigint as product_id,
        category_id::bigint as category_id,
        brand_id::bigint as brand_id,
        price::double precision as price,
        price_bucket::smallint as price_bucket,
        quantity::integer as quantity,
        device_type::text as device_type,
        source::text as source,
        campaign_id::text as campaign_id,
        page_context::text as page_context,
        rank_position::integer as rank_position,
        order_id::text as order_id,
        payload_hash::text as payload_hash,
        schema_version::smallint as schema_version,
        false as is_stream_processed
    from {{ source('staging', 'behavior_events') }}
),
streamed as (
    select
        event_id::text as event_id,
        event_timestamp,
        processed_timestamp as created_ts,
        processed_timestamp as ingestion_ts,
        user_id::bigint as user_id,
        null::text as session_id,
        null::text as request_id,
        null::text as impression_id,
        event_type::text as event_type,
        event_type_id::smallint as event_type_id,
        product_id::bigint as product_id,
        category_id::bigint as category_id,
        brand_id::bigint as brand_id,
        price::double precision as price,
        price_bucket::smallint as price_bucket,
        1::integer as quantity,
        'unknown'::text as device_type,
        source_topic::text as source,
        'none'::text as campaign_id,
        'stream'::text as page_context,
        null::integer as rank_position,
        null::text as order_id,
        payload_hash::text as payload_hash,
        2::smallint as schema_version,
        true as is_stream_processed
    from {{ source('staging', 'stream_behavior_events') }}
),
deduped as (
    select *
    from (
        select
            *,
            row_number() over (
                partition by event_id
                order by is_stream_processed desc, ingestion_ts desc
            ) as rn
        from (
            select * from historical
            union all
            select * from streamed
        ) unioned
    ) ranked
    where rn = 1
)
select
    event_id,
    event_timestamp,
    created_ts,
    ingestion_ts,
    user_id,
    session_id,
    request_id,
    impression_id,
    event_type,
    event_type_id,
    product_id,
    category_id,
    brand_id,
    price,
    price_bucket,
    quantity,
    device_type,
    source,
    campaign_id,
    page_context,
    rank_position,
    order_id,
    payload_hash,
    schema_version,
    is_stream_processed
from deduped
