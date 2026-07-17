from __future__ import annotations

from typing import Sequence

import numpy as np

from config import DistributionConfig
from domain import Product, User


class DataSkewProblem:
    """Create city, category, popularity, and exposure skew."""

    def __init__(
        self,
        rng: np.random.Generator,
        distribution: DistributionConfig,
        n_categories: int,
    ):
        self.rng = rng
        self.distribution = distribution
        self.n_categories = n_categories

    def city_weights(self) -> list[float]:
        city_count = len(self.distribution.cities)
        if city_count == 1:
            return [1.0]
        remainder = (1 - self.distribution.top_city_ratio) / (city_count - 1)
        return [
            self.distribution.top_city_ratio
            if city == self.distribution.top_city
            else remainder
            for city in self.distribution.cities
        ]

    def category_id(self) -> int:
        if self.rng.random() < self.distribution.top_category_ratio:
            return 1
        if self.n_categories == 1:
            return 1
        return int(self.rng.integers(2, self.n_categories + 1))

    def popularity_weight(self) -> float:
        return round(float(0.3 + self.rng.pareto(2.5)), 6)

    def select_candidates(
        self, user: User, products: Sequence[Product], count: int
    ) -> list[Product]:
        weights = np.asarray(
            [
                product.popularity_weight
                * (2.4 if product.category_id == user.preferred_category_id else 1.0)
                * (1.4 if product.brand_id == user.preferred_brand_id else 1.0)
                for product in products
            ],
            dtype=np.float64,
        )
        weights /= weights.sum()
        indexes = self.rng.choice(
            len(products), size=min(count, len(products)), replace=False, p=weights
        )
        return [products[int(index)] for index in indexes]
