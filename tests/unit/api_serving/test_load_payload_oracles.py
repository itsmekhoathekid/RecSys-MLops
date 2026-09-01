from __future__ import annotations

import pytest

from tests.load.payload_oracles import online_feature_payload_error


@pytest.mark.parametrize(
    "user_sequence",
    [
        {},
        {"hist_item_ids": [7, 8]},
    ],
)
def test_online_feature_oracle_accepts_cold_and_warm_users(
    user_sequence: dict,
) -> None:
    assert (
        online_feature_payload_error(
            {
                "user_sequence": user_sequence,
                "item_features": {"456": {"brand": "Acme"}},
            }
        )
        is None
    )


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        ([], "invalid online feature payload"),
        ({"item_features": {"456": {}}}, "missing or invalid user_sequence"),
        (
            {"user_sequence": [], "item_features": {"456": {}}},
            "missing or invalid user_sequence",
        ),
        ({"user_sequence": {}}, "missing or invalid item_features"),
        (
            {"user_sequence": {}, "item_features": []},
            "missing or invalid item_features",
        ),
        ({"user_sequence": {}, "item_features": {}}, "empty item_features"),
    ],
)
def test_online_feature_oracle_rejects_malformed_or_empty_item_features(
    body: object,
    expected_error: str,
) -> None:
    assert online_feature_payload_error(body) == expected_error
