with experiment_funnel as (
    select
        date(i.impression_timestamp) as metric_date,
        i.experiment_id,
        i.variant,
        coalesce(u.user_segment, 'unknown') as user_segment,
        count(distinct i.user_id) as users,
        count(*) as impressions,
        sum(i.has_click) as clicks,
        sum(i.has_cart) as carts,
        sum(i.has_purchase) as purchases
    from {{ ref('fct_recommendation_impressions') }} i
    left join {{ ref('stg_users') }} u on i.user_id = u.user_id
    where i.experiment_id is not null and i.variant is not null
    group by 1, 2, 3, 4
)
select
    *,
    cast(clicks as double) / nullif(impressions, 0) as ctr,
    cast(purchases as double) / nullif(impressions, 0) as conversion_rate
from experiment_funnel
