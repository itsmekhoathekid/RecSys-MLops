select
    impression_id,
    request_id,
    cast(user_id as bigint) as user_id,
    session_id,
    cast(impression_timestamp as timestamp(6)) as impression_timestamp,
    cast(candidate_product_id as bigint) as product_id,
    cast(rank_position as integer) as rank_position,
    candidate_source,
    cast(retrieval_score as double) as retrieval_score,
    cast(ranking_score as double) as ranking_score,
    surface,
    coalesce(is_clicked, false) as source_is_clicked,
    analytics_synced_at
from {{ source('silver', 'clean_impressions') }}

