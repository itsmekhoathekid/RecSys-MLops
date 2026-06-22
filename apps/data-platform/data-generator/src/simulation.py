from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

import numpy as np

from behavior import (
    BehaviorContext,
    BehaviorProbabilityModel,
    SessionState,
    SessionStateMachine,
)
from challenges import event_payload_hash
from config import GeneratorConfig
from domain import (
    BehaviorEvent,
    GeneratedData,
    Impression,
    Order,
    OrderItem,
    Product,
    ProductSnapshot,
    RecommendationRequest,
    Session,
    User,
    UserPreference,
)
from drift.controller import DriftController
from randomness import DeterministicIds, utc_datetime, weighted_index


MONEY = Decimal("0.01")


class RecsysSimulation:
    def __init__(self, config: GeneratorConfig):
        self.config = config
        self.rng = np.random.default_rng(config.seed)
        self.ids = DeterministicIds(config.seed)
        self.probabilities = BehaviorProbabilityModel(config.session_behavior)
        self.state_machine = SessionStateMachine(self.rng, self.probabilities)
        self.drift_controller = DriftController(config.drift)

    def generate(self) -> GeneratedData:
        users, preferences = self._generate_users()
        products, snapshots = self._generate_products()

        sessions: list[Session] = []
        requests: list[RecommendationRequest] = []
        impressions: list[Impression] = []
        events: list[BehaviorEvent] = []
        orders: list[Order] = []
        order_items: list[OrderItem] = []

        challenge_multiplier = (
            1
            + self.config.challenges.duplicate_event_rate
            + self.config.challenges.conflicting_duplicate_rate
        )
        clean_target = int(
            round(self.config.traffic.target_behavior_events / challenge_multiplier)
        )
        session_index = 0
        traffic_users = [user for user in users if user.is_active]

        while len(events) < clean_target:
            user = traffic_users[int(self.rng.integers(0, len(traffic_users)))]
            session_day = self._sample_history_day()
            session_start = self._sample_session_start(session_day, session_index)
            generated = self._generate_session(user, products, session_start)
            session, new_requests, new_impressions, new_events, new_orders, new_items = (
                generated
            )
            sessions.append(session)
            requests.extend(new_requests)
            impressions.extend(new_impressions)
            events.extend(new_events)
            orders.extend(new_orders)
            order_items.extend(new_items)
            session_index += 1

        last_active_by_user: dict[int, datetime] = {}
        for session in sessions:
            last_active_by_user[session.user_id] = max(
                session.session_end_ts,
                last_active_by_user.get(session.user_id, session.session_end_ts),
            )
        users = [
            replace(
                user,
                last_active_ts=last_active_by_user.get(user.user_id, user.last_active_ts),
                updated_ts=last_active_by_user.get(user.user_id, user.updated_ts),
            )
            for user in users
        ]

        return GeneratedData(
            users=users,
            user_preferences=preferences,
            products=products,
            product_snapshots=snapshots,
            sessions=sessions,
            recommendation_requests=requests,
            impressions=impressions,
            behavior_events=events,
            orders=orders,
            order_items=order_items,
        )

    def _generate_users(self) -> tuple[list[User], list[UserPreference]]:
        users: list[User] = []
        preferences: list[UserPreference] = []
        start_ts = utc_datetime(self.config.history_start_date)
        city_weights = self._city_weights()

        for user_id in range(1, self.config.entities.n_users + 1):
            city = self.config.distribution.cities[
                weighted_index(self.rng, city_weights)
            ]
            category = self._sample_category_id()
            brand = int(self.rng.integers(1, self.config.entities.n_brands + 1))
            signup_days_before = int(self.rng.integers(1, 366))
            signup_ts = start_ts - timedelta(days=signup_days_before)
            segment = str(
                self.rng.choice(["new", "regular", "vip"], p=[0.2, 0.7, 0.1])
            )
            lifecycle = str(
                self.rng.choice(["active", "dormant", "churned"], p=[0.84, 0.12, 0.04])
            )
            user = User(
                user_id=user_id,
                signup_ts=signup_ts,
                signup_channel=str(
                    self.rng.choice(["organic", "ads", "referral"], p=[0.55, 0.3, 0.15])
                ),
                city=city,
                country="VN",
                segment=segment,
                age_bucket=int(self.rng.integers(1, 8)),
                preferred_category_id=category,
                preferred_brand_id=brand,
                price_sensitivity=round(float(self.rng.beta(2, 2)), 6),
                user_lifecycle_state=lifecycle,
                last_active_ts=signup_ts,
                is_active=lifecycle != "churned",
                created_ts=signup_ts,
                updated_ts=signup_ts,
            )
            users.append(user)

            preference_categories = [category]
            while len(preference_categories) < self.config.entities.preferences_per_user:
                candidate = self._sample_category_id()
                if candidate not in preference_categories:
                    preference_categories.append(candidate)
            raw_weights = self.rng.dirichlet(
                np.ones(self.config.entities.preferences_per_user)
            )
            for index, preference_category in enumerate(preference_categories):
                preferences.append(
                    UserPreference(
                        user_id=user_id,
                        category_id=preference_category,
                        brand_id=brand if index == 0 else None,
                        preference_weight=round(float(raw_weights[index]), 6),
                        source="generated",
                        created_ts=signup_ts,
                        updated_ts=signup_ts,
                    )
                )
        return users, preferences

    def _generate_products(
        self,
    ) -> tuple[list[Product], list[ProductSnapshot]]:
        products: list[Product] = []
        snapshots: list[ProductSnapshot] = []
        valid_from = utc_datetime(self.config.history_start_date) - timedelta(days=30)

        for product_id in range(1, self.config.entities.n_products + 1):
            category_id = self._sample_category_id()
            brand_id = int(self.rng.integers(1, self.config.entities.n_brands + 1))
            price_value = float(np.exp(self.rng.normal(4.5, 0.9)))
            base_price = Decimal(str(min(max(price_value, 5), 3000))).quantize(MONEY)
            discount = Decimal(str(round(float(self.rng.uniform(0, 0.2)), 4)))
            current_price = (base_price * (Decimal("1") - discount)).quantize(
                MONEY, rounding=ROUND_HALF_UP
            )
            price_bucket = min(max(int(float(current_price) // 100) + 1, 1), 10)
            category_code = (
                "electronics"
                if category_id == 1
                else f"category_{category_id:02d}"
            )
            brand_name = f"brand_{brand_id:03d}"
            popularity = round(float(0.3 + self.rng.pareto(2.5)), 6)
            created_ts = valid_from - timedelta(
                days=int(self.rng.integers(1, 365))
            )
            product = Product(
                product_id=product_id,
                product_name=f"product_{product_id:05d}",
                category_id=category_id,
                category_code=category_code,
                brand_id=brand_id,
                brand_name=brand_name,
                base_price=base_price,
                current_price=current_price,
                price_bucket=price_bucket,
                popularity_weight=popularity,
                is_active=True,
                created_ts=created_ts,
                updated_ts=valid_from,
            )
            products.append(product)
            snapshots.append(
                ProductSnapshot(
                    product_id=product_id,
                    valid_from=valid_from,
                    valid_to=None,
                    category_id=category_id,
                    category_code=category_code,
                    brand_id=brand_id,
                    brand_name=brand_name,
                    current_price=current_price,
                    price_bucket=price_bucket,
                    is_active=True,
                    created_ts=valid_from,
                )
            )
        return products, snapshots

    def _generate_session(
        self, user: User, products: list[Product], session_start: datetime
    ) -> tuple[
        Session,
        list[RecommendationRequest],
        list[Impression],
        list[BehaviorEvent],
        list[Order],
        list[OrderItem],
    ]:
        session_id = self.ids.next("session")
        source = str(self.rng.choice(["app", "web"], p=[0.62, 0.38]))
        device = str(
            self.rng.choice(["mobile", "desktop", "tablet"], p=[0.72, 0.23, 0.05])
        )
        is_campaign = bool(self.rng.random() < 0.18)
        campaign_id = f"campaign_{int(self.rng.integers(1, 6)):02d}" if is_campaign else None
        request_count = int(
            self.rng.integers(
                self.config.traffic.requests_per_session_min,
                self.config.traffic.requests_per_session_max + 1,
            )
        )

        requests: list[RecommendationRequest] = []
        impressions: list[Impression] = []
        events: list[BehaviorEvent] = []
        orders: list[Order] = []
        order_items: list[OrderItem] = []
        cursor = session_start
        end_reason = "bounce"

        for _ in range(request_count):
            request_id = self.ids.next("request")
            surface = str(
                self.rng.choice(["homepage", "pdp", "cart", "search"], p=[0.5, 0.2, 0.1, 0.2])
            )
            schema_version = (
                1
                if cursor.date() < self.config.schema_evolution.change_date
                else 2
            )
            request = RecommendationRequest(
                request_id=request_id,
                user_id=user.user_id,
                session_id=session_id,
                request_timestamp=cursor,
                surface=surface,
                context_product_id=None,
                context_category_id=user.preferred_category_id,
                device_type=device if schema_version == 2 else None,
                source=source,
                campaign_id=campaign_id if schema_version == 2 else None,
                created_ts=cursor + timedelta(seconds=1),
                schema_version=schema_version,
            )
            requests.append(request)

            candidate_count = int(
                self.rng.integers(
                    self.config.traffic.impressions_per_request_min,
                    self.config.traffic.impressions_per_request_max + 1,
                )
            )
            candidates = self._select_candidates(user, products, candidate_count)
            request_impressions: list[Impression] = []

            for rank_position, product in enumerate(candidates, start=1):
                impression_id = self.ids.next("impression")
                impression_ts = cursor + timedelta(seconds=rank_position)
                context = BehaviorContext(
                    rank_position=rank_position,
                    is_campaign=is_campaign,
                    drift_factor=self.drift_controller.get_factor(impression_ts),
                )
                states = self.state_machine.run(user, product, context)
                clicked = SessionState.VIEW in states
                impression = Impression(
                    impression_id=impression_id,
                    request_id=request_id,
                    user_id=user.user_id,
                    session_id=session_id,
                    impression_timestamp=impression_ts,
                    candidate_product_id=product.product_id,
                    rank_position=rank_position,
                    candidate_source=(
                        "category"
                        if product.category_id == user.preferred_category_id
                        else "popular"
                    ),
                    retrieval_score=round(
                        float(product.popularity_weight / (rank_position + 1)), 6
                    ),
                    ranking_score=round(
                        float(self.probabilities.p_view(user, product, context)), 6
                    ),
                    surface=surface,
                    is_clicked=clicked,
                    created_ts=impression_ts + timedelta(seconds=1),
                    schema_version=schema_version,
                )
                request_impressions.append(impression)

                event_cursor = impression_ts + timedelta(seconds=1)
                for state in states:
                    if state not in {
                        SessionState.VIEW,
                        SessionState.CART,
                        SessionState.PURCHASE,
                    }:
                        continue
                    event_type = state.value
                    order_id = None
                    quantity = 1
                    if state == SessionState.PURCHASE:
                        order_id = self.ids.next("order")
                        quantity = int(self.rng.integers(1, 4))

                    event = self._make_event(
                        event_type=event_type,
                        timestamp=event_cursor,
                        user=user,
                        product=product,
                        session_id=session_id,
                        request=request,
                        impression=impression,
                        order_id=order_id,
                        quantity=quantity,
                        device=device,
                        source=source,
                        campaign_id=campaign_id,
                    )
                    events.append(event)
                    event_cursor += timedelta(seconds=int(self.rng.integers(2, 21)))

                    if state == SessionState.PURCHASE and order_id is not None:
                        order, item = self._make_order(
                            order_id,
                            user,
                            product,
                            session_id,
                            event.event_timestamp,
                            quantity,
                            campaign_id,
                        )
                        orders.append(order)
                        order_items.append(item)
                        end_reason = "purchase"
                    elif state == SessionState.CART and end_reason != "purchase":
                        end_reason = "abandon"
                    elif state == SessionState.VIEW and end_reason == "bounce":
                        end_reason = "browse"

            impressions.extend(request_impressions)
            cursor += timedelta(seconds=int(self.rng.integers(30, 181)))

        session_end = max(
            [cursor]
            + [event.event_timestamp + timedelta(seconds=1) for event in events]
        )
        session = Session(
            session_id=session_id,
            user_id=user.user_id,
            session_start_ts=session_start,
            session_end_ts=session_end,
            entry_source=source,
            device_type=device,
            campaign_id=campaign_id,
            session_end_reason=end_reason,
            created_ts=session_start + timedelta(seconds=1),
            updated_ts=session_end + timedelta(seconds=1),
        )
        return session, requests, impressions, events, orders, order_items

    def _make_event(
        self,
        event_type: str,
        timestamp: datetime,
        user: User,
        product: Product,
        session_id,
        request: RecommendationRequest,
        impression: Impression,
        order_id,
        quantity: int,
        device: str,
        source: str,
        campaign_id: str | None,
    ) -> BehaviorEvent:
        event = BehaviorEvent(
            event_id=self.ids.next("event"),
            event_timestamp=timestamp,
            created_ts=timestamp + timedelta(seconds=int(self.rng.integers(1, 11))),
            ingestion_ts=timestamp + timedelta(seconds=int(self.rng.integers(12, 31))),
            user_id=user.user_id,
            session_id=session_id,
            request_id=request.request_id,
            impression_id=impression.impression_id,
            event_type=event_type,
            product_id=product.product_id,
            category_id=product.category_id,
            brand_id=product.brand_id,
            price=product.current_price,
            price_bucket=product.price_bucket,
            quantity=quantity,
            device_type=device,
            source=source,
            campaign_id=campaign_id,
            page_context=request.surface,
            rank_position=impression.rank_position,
            order_id=order_id,
            payload_hash="",
            event_date=timestamp.date(),
            schema_version=request.schema_version,
            drift_enabled=self.config.drift.enabled,
            drift_scenario=self.drift_controller.scenario,
            drift_phase=self.drift_controller.get_phase(timestamp),
            drift_factor=self.drift_controller.get_factor(timestamp),
        )
        return replace(event, payload_hash=event_payload_hash(event))

    def _make_order(
        self,
        order_id,
        user: User,
        product: Product,
        session_id,
        timestamp: datetime,
        quantity: int,
        campaign_id: str | None,
    ) -> tuple[Order, OrderItem]:
        gross = (product.current_price * quantity).quantize(MONEY)
        discount_rate = Decimal("0.10") if campaign_id else Decimal("0.00")
        discount = (gross * discount_rate).quantize(MONEY)
        net = (gross - discount).quantize(MONEY)
        item = OrderItem(
            order_item_id=self.ids.next("order_item"),
            order_id=order_id,
            product_id=product.product_id,
            quantity=quantity,
            unit_price=product.current_price,
            discount_amount=discount,
            line_amount=net,
            created_ts=timestamp + timedelta(seconds=1),
        )
        order = Order(
            order_id=order_id,
            user_id=user.user_id,
            session_id=session_id,
            order_timestamp=timestamp,
            status="paid",
            gross_amount=gross,
            discount_amount=discount,
            net_amount=net,
            coupon_code="CAMPAIGN10" if campaign_id else None,
            payment_method=str(self.rng.choice(["cod", "card", "wallet"])),
            shipping_city=user.city,
            paid_ts=timestamp + timedelta(seconds=30),
            cancelled_ts=None,
            refunded_ts=None,
            created_ts=timestamp + timedelta(seconds=1),
            updated_ts=timestamp + timedelta(seconds=30),
            drift_enabled=self.config.drift.enabled,
            drift_scenario=self.drift_controller.scenario,
            drift_phase=self.drift_controller.get_phase(timestamp),
            drift_factor=self.drift_controller.get_factor(timestamp),
        )
        return order, item

    def _select_candidates(
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

    def _sample_history_day(self) -> date:
        offset = int(self.rng.integers(0, self.config.history_days))
        return self.config.history_start_date + timedelta(days=offset)

    def _sample_session_start(self, day: date, index: int) -> datetime:
        hour_weights = np.ones(24, dtype=np.float64)
        for window in self.config.burst_windows:
            hour_weights[window.start_hour : window.end_hour] *= window.traffic_weight
        hour_weights /= hour_weights.sum()
        hour = int(self.rng.choice(24, p=hour_weights))
        minute = int(self.rng.integers(0, 60))
        second = int((index * 17 + self.config.seed) % 60)
        return datetime(
            day.year,
            day.month,
            day.day,
            hour,
            minute,
            second,
            tzinfo=timezone.utc,
        )

    def _sample_category_id(self) -> int:
        if self.rng.random() < self.config.distribution.top_category_ratio:
            return 1
        if self.config.entities.n_categories == 1:
            return 1
        return int(self.rng.integers(2, self.config.entities.n_categories + 1))

    def _city_weights(self) -> list[float]:
        city_count = len(self.config.distribution.cities)
        if city_count == 1:
            return [1.0]
        remainder = (1 - self.config.distribution.top_city_ratio) / (city_count - 1)
        return [
            self.config.distribution.top_city_ratio
            if city == self.config.distribution.top_city
            else remainder
            for city in self.config.distribution.cities
        ]
