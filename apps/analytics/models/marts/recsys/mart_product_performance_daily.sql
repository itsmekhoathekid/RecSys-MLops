select
    date(i.impression_timestamp) as metric_date,
    i.product_id,
    p.category_id,
    p.category_code,
    p.brand_id,
    p.brand_name,
    count(*) as impressions,
    sum(i.has_click) as clicks,
    sum(i.has_cart) as carts,
    sum(i.has_purchase) as attributed_purchases,
    cast(sum(i.has_click) as double) / nullif(count(*), 0) as ctr,
    cast(sum(i.has_purchase) as double) / nullif(count(*), 0) as conversion_rate
from {{ ref('fct_recommendation_impressions') }} i
left join {{ ref('dim_product') }} p
  on i.product_id = p.product_id
 and i.impression_timestamp >= p.valid_from
 and (p.valid_to is null or i.impression_timestamp < p.valid_to)
group by 1, 2, 3, 4, 5, 6

