from __future__ import annotations

# Streaming features are computed by PyFlink jobs in pipelines/data_pipeline.
# They write Redis payloads with names that match the offline Feast feature
# views. This placeholder keeps the Feast repo layout explicit for future
# native Feast stream feature views.

STREAM_FEATURE_VIEW_NAMES = [
    "user_sequence_features",
    "user_aggregate_features",
    "item_features",
    "category_realtime_features",
    "brand_realtime_features",
]

