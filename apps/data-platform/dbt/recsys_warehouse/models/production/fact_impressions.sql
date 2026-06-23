{{ config(schema='production') }}

select
    impression_id::text as impression_id,
    request_id::text as request_id,
    user_id::bigint as user_id,
    session_id::text as session_id,
    impression_timestamp,
    candidate_product_id::bigint as candidate_product_id,
    rank_position::integer as rank_position,
    candidate_source::text as candidate_source,
    retrieval_score::double precision as retrieval_score,
    ranking_score::double precision as ranking_score,
    surface::text as surface,
    is_clicked::boolean as is_clicked,
    created_ts,
    schema_version::smallint as schema_version
from {{ source('staging', 'impressions') }}

