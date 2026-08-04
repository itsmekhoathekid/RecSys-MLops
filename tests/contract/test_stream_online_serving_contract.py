from __future__ import annotations

from recsys_online_feature_api.service import normalize_realtime_user_features


def test_realtime_flink_sequence_matches_online_feature_contract() -> None:
    payload = normalize_realtime_user_features(
        {
            "item_ids": [15],
            "event_type_ids": [1],
            "category_ids": [6],
            "brand_ids": [3],
            "price_bucket_ids": [2],
            "event_timestamps": ["2026-07-13T06:15:26Z"],
            "request_ids": ["web-event-123"],
            "impression_ids": [""],
            "sequence_length": 1,
            "max_history_length": 50,
            "feature_version": "bst_sequence_v2",
        },
        {"views_30m": 1, "carts_30m": 0, "purchases_24h": 0},
    )

    assert payload["hist_item_ids"] == [15]
    assert payload["hist_event_type_ids"] == [1]
    assert payload["hist_request_ids"] == ["web-event-123"]
    assert payload["hist_length"] == 1
    assert payload["views_30m"] == 1
