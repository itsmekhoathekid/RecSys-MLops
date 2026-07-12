with funnel as (
    select
        metric_date,
        count(distinct session_id) as sessions,
        count(distinct user_id) as users,
        sum(impressions) as impressions,
        sum(clicks) as clicks,
        sum(carts) as carts,
        sum(purchases) as purchases
    from {{ ref('int_session_funnels') }}
    group by 1
), revenue as (
    select
        date(order_timestamp) as metric_date,
        sum(case when is_valid_purchase then line_amount else decimal '0.00' end) as revenue
    from {{ ref('fct_order_items') }}
    group by 1
)
select
    f.*,
    coalesce(r.revenue, decimal '0.00') as revenue,
    cast(f.clicks as double) / nullif(f.impressions, 0) as ctr,
    cast(f.purchases as double) / nullif(f.clicks, 0) as click_to_purchase_cvr,
    cast(f.purchases as double) / nullif(f.impressions, 0) as impression_to_purchase_cvr
from funnel f
left join revenue r on f.metric_date = r.metric_date
