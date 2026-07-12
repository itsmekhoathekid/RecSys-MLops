select
    date(impression_timestamp) as metric_date,
    session_id,
    user_id,
    count(*) as impressions,
    sum(has_click) as clicks,
    sum(has_cart) as carts,
    sum(has_purchase) as purchases
from {{ ref('int_impression_outcomes') }}
group by 1, 2, 3

