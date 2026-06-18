from __future__ import annotations

try:
    from feast import Entity
except ImportError:  # pragma: no cover - used when Feast is not installed
    Entity = None


if Entity is not None:
    user = Entity(name="user", join_keys=["user_id"])
    product = Entity(name="product", join_keys=["product_id"])
    category = Entity(name="category", join_keys=["category_id"])
    brand = Entity(name="brand", join_keys=["brand_id"])
else:
    user = product = category = brand = None

