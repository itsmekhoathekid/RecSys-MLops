from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd


def build_item_features(
    clean_events: pd.DataFrame,
    products: pd.DataFrame,
    alpha: float = 1.0,
    beta: float = 10.0,
    feature_version: str = "item_features_v1",
) -> pd.DataFrame:
    if clean_events.empty:
        return pd.DataFrame()
    events = clean_events.copy()
    events["event_timestamp"] = pd.to_datetime(events["event_timestamp"], utc=True)
    product_meta = products.set_index("product_id")
    rows: list[dict] = []
    for product_id, group in events.sort_values("event_timestamp").groupby("product_id"):
        group = group.reset_index(drop=True)
        meta = product_meta.loc[product_id] if product_id in product_meta.index else {}
        for _, event in group.iterrows():
            ts = event["event_timestamp"]
            w1h = ts - pd.Timedelta(hours=1)
            w24h = ts - pd.Timedelta(hours=24)
            w7d = ts - pd.Timedelta(days=7)
            h1 = group[(group["event_timestamp"] > w1h) & (group["event_timestamp"] <= ts)]
            h24 = group[(group["event_timestamp"] > w24h) & (group["event_timestamp"] <= ts)]
            d7 = group[(group["event_timestamp"] > w7d) & (group["event_timestamp"] <= ts)]
            views_7d = int((d7["event_type"] == "view").sum())
            purchases_7d = int((d7["event_type"] == "purchase").sum())
            conversion_rate = (purchases_7d + alpha) / (views_7d + beta)
            popularity_score = (
                int((h24["event_type"] == "view").sum())
                + 3 * int((h24["event_type"] == "cart").sum())
                + 10 * int((h24["event_type"] == "purchase").sum())
            )
            rows.append(
                {
                    "product_id": int(product_id),
                    "feature_timestamp": ts,
                    "event_timestamp": ts,
                    "category_id": int(getattr(meta, "category_id", event["category_id"])),
                    "brand_id": int(getattr(meta, "brand_id", event["brand_id"])),
                    "price_bucket": int(getattr(meta, "price_bucket", event["price_bucket"])),
                    "is_active": bool(getattr(meta, "is_active", True)),
                    "views_1h": int((h1["event_type"] == "view").sum()),
                    "views_24h": int((h24["event_type"] == "view").sum()),
                    "carts_1h": int((h1["event_type"] == "cart").sum()),
                    "carts_24h": int((h24["event_type"] == "cart").sum()),
                    "purchases_24h": int((h24["event_type"] == "purchase").sum()),
                    "purchases_7d": purchases_7d,
                    "conversion_rate_7d": float(conversion_rate),
                    "popularity_score": float(popularity_score),
                    "aggregation_window_end_ts": ts,
                    "watermark_ts": ts,
                    "created_timestamp": datetime.now(timezone.utc),
                    "feature_version": feature_version,
                }
            )
    return pd.DataFrame(rows)

