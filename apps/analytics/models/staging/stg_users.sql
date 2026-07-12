select
    cast(user_id as bigint) as user_id,
    cast(signup_ts as timestamp(6)) as signup_ts,
    signup_channel,
    city,
    country,
    segment as user_segment,
    cast(age_bucket as integer) as age_bucket,
    user_lifecycle_state,
    is_active,
    analytics_synced_at
from {{ source('silver', 'users') }}
