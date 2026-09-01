from __future__ import annotations


def online_feature_payload_error(body: object) -> str | None:
    """Return a Locust failure reason for an invalid online-feature response."""
    if not isinstance(body, dict):
        return "invalid online feature payload"

    user_sequence = body.get("user_sequence")
    if not isinstance(user_sequence, dict):
        return "missing or invalid user_sequence"

    item_features = body.get("item_features")
    if not isinstance(item_features, dict):
        return "missing or invalid item_features"
    if not item_features:
        return "empty item_features"

    return None
