with events as (
    select
        impression_id,
        max(case when event_type = 'click' then 1 else 0 end) as has_click,
        max(case when event_type = 'cart' then 1 else 0 end) as has_cart,
        max(case when event_type = 'purchase' then 1 else 0 end) as has_purchase,
        min(case when event_type = 'click' then event_timestamp end) as clicked_at,
        min(case when event_type = 'cart' then event_timestamp end) as carted_at,
        min(case when event_type = 'purchase' then event_timestamp end) as purchased_at
    from {{ ref('stg_behavior_events') }}
    where impression_id is not null
    group by 1
), requests as (
    select request_id, experiment_id, variant
    from {{ ref('stg_recommendation_requests') }}
)
select
    i.*,
    r.experiment_id,
    r.variant,
    greatest(coalesce(e.has_click, 0), case when i.source_is_clicked then 1 else 0 end) as has_click,
    coalesce(e.has_cart, 0) as has_cart,
    coalesce(e.has_purchase, 0) as has_purchase,
    e.clicked_at,
    e.carted_at,
    e.purchased_at
from {{ ref('stg_impressions') }} i
left join events e on i.impression_id = e.impression_id
left join requests r on i.request_id = r.request_id
