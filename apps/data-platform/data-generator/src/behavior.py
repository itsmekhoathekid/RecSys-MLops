from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from config import SessionBehaviorConfig
from domain import Product, User


class SessionState(str, Enum):
    IMPRESSION = "impression"
    VIEW = "view"
    CART = "cart"
    PURCHASE = "purchase"
    ABANDON = "abandon"
    LEAVE = "leave"


@dataclass(frozen=True)
class BehaviorContext:
    rank_position: int
    is_campaign: bool
    drift_factor: float = 1.0


class BehaviorProbabilityModel:
    def __init__(self, config: SessionBehaviorConfig):
        self.config = config

    def p_view(self, user: User, product: Product, context: BehaviorContext) -> float:
        probability = self.config.view_after_impression_base
        if product.category_id == user.preferred_category_id:
            probability *= 1.8
        if product.brand_id == user.preferred_brand_id:
            probability *= 1.3
        probability *= 0.7 + min(product.popularity_weight, 2.0) * 0.3
        probability *= 1 / (1 + 0.035 * max(context.rank_position - 1, 0))
        if context.is_campaign:
            probability *= 1.25
        return min(probability, 0.95)

    def p_cart(self, user: User, product: Product, context: BehaviorContext) -> float:
        probability = self.config.cart_after_view_base
        if product.category_id == user.preferred_category_id:
            probability *= 1.35
        if product.price_bucket >= 8 and user.price_sensitivity > 0.7:
            probability *= 0.5
        if user.segment == "vip":
            probability *= 1.4
        if context.is_campaign:
            probability *= 1.15
        return min(probability, 0.9)

    def p_purchase(
        self, user: User, product: Product, context: BehaviorContext
    ) -> float:
        probability = self.config.purchase_after_cart_base
        if product.price_bucket >= 8 and user.price_sensitivity > 0.7:
            probability *= 0.4
        if user.segment == "vip":
            probability *= 1.5
        if context.is_campaign:
            probability *= 1.2
        probability *= context.drift_factor
        return min(probability, 0.95)


class SessionStateMachine:
    def __init__(
        self, rng: np.random.Generator, probabilities: BehaviorProbabilityModel
    ):
        self.rng = rng
        self.probabilities = probabilities

    def run(
        self, user: User, product: Product, context: BehaviorContext
    ) -> list[SessionState]:
        states = [SessionState.IMPRESSION]
        if self.rng.random() >= self.probabilities.p_view(user, product, context):
            return states + [SessionState.LEAVE]
        states.append(SessionState.VIEW)
        if self.rng.random() >= self.probabilities.p_cart(user, product, context):
            return states + [SessionState.LEAVE]
        states.append(SessionState.CART)
        if self.rng.random() < self.probabilities.p_purchase(user, product, context):
            states.append(SessionState.PURCHASE)
        else:
            states.append(SessionState.ABANDON)
        return states
