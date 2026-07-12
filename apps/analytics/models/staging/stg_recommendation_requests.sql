select
    request_id,
    cast(user_id as bigint) as user_id,
    session_id,
    cast(request_timestamp as timestamp(6)) as request_timestamp,
    surface,
    device_type,
    source,
    campaign_id,
    nullif(json_extract_scalar(try(json_parse(request_context)), '$.experiment_id'), '') as experiment_id,
    nullif(json_extract_scalar(try(json_parse(request_context)), '$.variant'), '') as variant,
    analytics_synced_at
from {{ source('silver', 'clean_recommendation_requests') }}

