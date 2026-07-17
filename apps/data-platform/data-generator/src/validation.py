from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import timedelta
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from config import GeneratorConfig
from domain import GeneratedData
from drift.controller import DriftController
from drift.reporting import (
    DRIFT_ALERT_SCHEMA,
    FEATURE_HEALTH_SCHEMA,
    USER_DAILY_FEATURE_SCHEMA,
)
from schemas import SCHEMAS
from sink import read_table


@dataclass(frozen=True)
class ValidationResult:
    errors: list[str]
    metrics: dict[str, Any]

    @property
    def passed(self) -> bool:
        return not self.errors


class InvariantValidator:
    def validate(
        self,
        data: GeneratedData,
        config: GeneratorConfig,
        emitted_event_count: int,
    ) -> ValidationResult:
        errors: list[str] = []
        user_ids = {row.user_id for row in data.users}
        user_active = {row.user_id: row.is_active for row in data.users}
        product_by_id = {row.product_id: row for row in data.products}
        session_by_id = {row.session_id: row for row in data.sessions}
        request_by_id = {row.request_id: row for row in data.recommendation_requests}
        impression_by_id = {row.impression_id: row for row in data.impressions}
        order_by_id = {row.order_id: row for row in data.orders}
        drift_controller = DriftController(config.drift)

        for preference in data.user_preferences:
            if preference.user_id not in user_ids:
                errors.append(f"preference references missing user {preference.user_id}")

        for session in data.sessions:
            if session.user_id not in user_ids:
                errors.append(f"session {session.session_id} references missing user")
            elif not user_active[session.user_id]:
                errors.append(f"session {session.session_id} belongs to inactive user")

        for request in data.recommendation_requests:
            session = session_by_id.get(request.session_id)
            if session is None or session.user_id != request.user_id:
                errors.append(f"request {request.request_id} has invalid session/user")

        for impression in data.impressions:
            request = request_by_id.get(impression.request_id)
            if (
                request is None
                or request.user_id != impression.user_id
                or request.session_id != impression.session_id
            ):
                errors.append(
                    f"impression {impression.impression_id} has invalid request linkage"
                )
            if impression.candidate_product_id not in product_by_id:
                errors.append(
                    f"impression {impression.impression_id} has invalid product"
                )

        canonical_events = {}
        for event in data.behavior_events:
            canonical_events.setdefault(event.event_id, event)
        for event in canonical_events.values():
            product = product_by_id.get(event.product_id)
            impression = (
                impression_by_id.get(event.impression_id)
                if event.impression_id is not None
                else None
            )
            if event.user_id not in user_ids or event.session_id not in session_by_id:
                errors.append(f"event {event.event_id} has invalid user/session")
            if product is None:
                errors.append(f"event {event.event_id} has invalid product")
            elif (
                event.category_id != product.category_id
                or event.brand_id != product.brand_id
                or event.price_bucket != product.price_bucket
            ):
                errors.append(f"event {event.event_id} metadata does not match product")
            if event.impression_id is not None and (
                impression is None
                or impression.request_id != event.request_id
                or impression.user_id != event.user_id
                or impression.candidate_product_id != event.product_id
            ):
                errors.append(f"event {event.event_id} has invalid impression linkage")
            if event.event_type == "purchase" and event.order_id not in order_by_id:
                errors.append(f"purchase event {event.event_id} has no valid order")
            if event.created_ts < event.event_timestamp:
                errors.append(f"event {event.event_id} created before event time")
            if min(
                event.user_id,
                event.product_id,
                event.category_id,
                event.brand_id,
                event.price_bucket,
            ) <= 0:
                errors.append(f"event {event.event_id} has non-positive categorical ID")
            expected_factor = drift_controller.get_factor(event.event_timestamp)
            expected_phase = drift_controller.get_phase(event.event_timestamp)
            if (
                event.drift_enabled != config.drift.enabled
                or event.drift_scenario != drift_controller.scenario
                or event.drift_phase != expected_phase
                or abs(event.drift_factor - expected_factor) > 1e-8
            ):
                errors.append(f"event {event.event_id} has invalid drift metadata")

        items_by_order: dict[Any, list] = defaultdict(list)
        for item in data.order_items:
            items_by_order[item.order_id].append(item)
            if item.order_id not in order_by_id:
                errors.append(f"order item {item.order_item_id} has invalid order")
            if item.product_id not in product_by_id:
                errors.append(f"order item {item.order_item_id} has invalid product")

        for order in data.orders:
            items = items_by_order.get(order.order_id, [])
            if not items:
                errors.append(f"order {order.order_id} has no items")
                continue
            gross = sum(
                (item.unit_price * item.quantity for item in items), Decimal("0.00")
            )
            discounts = sum(
                (item.discount_amount for item in items), Decimal("0.00")
            )
            net = sum((item.line_amount for item in items), Decimal("0.00"))
            if (
                gross != order.gross_amount
                or discounts != order.discount_amount
                or net != order.net_amount
            ):
                errors.append(f"order {order.order_id} totals do not match items")
            expected_factor = drift_controller.get_factor(order.order_timestamp)
            if (
                order.drift_enabled != config.drift.enabled
                or order.drift_scenario != drift_controller.scenario
                or order.drift_phase
                != drift_controller.get_phase(order.order_timestamp)
                or abs(order.drift_factor - expected_factor) > 1e-8
            ):
                errors.append(f"order {order.order_id} has invalid drift metadata")

        lower = int(
            config.traffic.target_behavior_events
            * (1 - config.traffic.target_tolerance)
        )
        upper = int(
            config.traffic.target_behavior_events
            * (1 + config.traffic.target_tolerance)
        )
        if not lower <= emitted_event_count <= upper:
            errors.append(
                f"behavior event count {emitted_event_count} outside [{lower}, {upper}]"
            )

        return ValidationResult(
            errors=errors,
            metrics={
                "canonical_event_count": len(canonical_events),
                "order_count": len(data.orders),
                "purchase_event_count": sum(
                    event.event_type == "purchase"
                    for event in canonical_events.values()
                ),
                "foreign_key_error_count": sum(
                    "invalid" in error or "missing" in error for error in errors
                ),
            },
        )


def duplicate_metrics(data: GeneratedData) -> dict[str, int]:
    counts = Counter(
        (event.event_id, event.payload_hash) for event in data.behavior_events
    )
    exact = sum(max(count - 1, 0) for count in counts.values())
    return {"exact_duplicate_rows": exact}


def validate_parquet_output(
    run_path: Path, config: GeneratorConfig | None = None
) -> ValidationResult:
    errors: list[str] = []
    row_counts: dict[str, int] = {}
    for table_name, expected_schema in SCHEMAS.items():
        try:
            table = read_table(run_path, table_name)
        except Exception as exc:
            errors.append(f"{table_name}: cannot read parquet: {exc}")
            continue
        row_counts[table_name] = table.num_rows
        if not table.schema.equals(expected_schema, check_metadata=False):
            drift_fields = {
                "drift_enabled",
                "drift_scenario",
                "drift_phase",
                "drift_factor",
            }
            legacy_schema = expected_schema
            if table_name in {"behavior_events", "orders"}:
                legacy_schema = pa.schema(
                    [field for field in expected_schema if field.name not in drift_fields]
                )
            legacy_allowed = (
                config is not None
                and not config.drift.enabled
                and table.schema.equals(legacy_schema, check_metadata=False)
            )
            if not legacy_allowed:
                errors.append(f"{table_name}: parquet schema mismatch")
    return ValidationResult(errors=errors, metrics={"row_counts": row_counts})


def validate_drift_output(
    run_path: Path, config: GeneratorConfig
) -> ValidationResult:
    if not config.drift.enabled:
        return ValidationResult(errors=[], metrics={"drift_enabled": False})

    errors: list[str] = []
    paths_and_schemas = {
        run_path / "reports/user_daily_features.parquet": USER_DAILY_FEATURE_SCHEMA,
        run_path / "monitoring/agg_feature_health_daily.parquet": (
            FEATURE_HEALTH_SCHEMA
        ),
        run_path / "monitoring/feature_drift_alerts.parquet": DRIFT_ALERT_SCHEMA,
    }
    row_counts: dict[str, int] = {}
    tables = {}
    for path, schema in paths_and_schemas.items():
        if not path.exists():
            errors.append(f"missing drift artifact: {path}")
            continue
        table = pq.read_table(path)
        tables[path.name] = table
        row_counts[path.name] = table.num_rows
        if not table.schema.equals(schema, check_metadata=False):
            errors.append(f"drift artifact schema mismatch: {path}")

    csv_path = run_path / "reports/drift_validation_report.csv"
    if not csv_path.exists():
        errors.append(f"missing drift artifact: {csv_path}")
    else:
        header = csv_path.read_text(encoding="utf-8").splitlines()[0]
        expected_header = (
            "date,feature_name,mean,stddev,psi_vs_baseline,"
            "drift_status,drift_factor"
        )
        if header != expected_header:
            errors.append("drift validation CSV header mismatch")

    health = tables.get("agg_feature_health_daily.parquet")
    if health is not None:
        rows = health.to_pylist()
        baseline_start = config.drift.baseline_start_date
        baseline_end = config.drift.baseline_end_date
        baseline_rows = [
            row
            for row in rows
            if baseline_start <= row["date"] <= baseline_end
        ]
        if not baseline_rows or any(
            row["drift_status"] != "baseline" for row in baseline_rows
        ):
            errors.append("baseline rows are missing or incorrectly classified")
        factors = [
            (row["date"], row["drift_factor"])
            for row in rows
            if row["feature_name"] == "f_user_purchase_count_90d"
        ]
        sorted_factors = [factor for _, factor in sorted(factors)]
        if any(
            current < previous
            for previous, current in zip(sorted_factors, sorted_factors[1:])
        ):
            errors.append("gradual drift factors are not monotonic")
        if any(
            factor < 1.0
            or factor > config.drift.purchase_probability_multiplier + 1e-8
            for factor in sorted_factors
        ):
            errors.append("drift factor outside configured bounds")

    features = tables.get("user_daily_features.parquet")
    if features is not None:
        for field in (
            "f_user_purchase_count_90d",
            "f_user_total_orders_90d",
            "f_user_interaction_count_90d",
        ):
            if any(value < 0 for value in features.column(field).to_pylist()):
                errors.append(f"negative rolling feature values in {field}")

    controller = DriftController(config.drift)
    try:
        event_rows = read_table(run_path, "behavior_events").to_pylist()
        order_rows = read_table(run_path, "orders").to_pylist()
    except Exception as exc:
        errors.append(f"cannot validate drift source metadata: {exc}")
        event_rows = []
        order_rows = []

    for row in event_rows:
        expected_factor = controller.get_factor(row["event_timestamp"])
        if (
            row["drift_enabled"] is not True
            or row["drift_scenario"] != config.drift.scenario
            or row["drift_phase"] != controller.get_phase(row["event_timestamp"])
            or abs(row["drift_factor"] - expected_factor) > 1e-8
        ):
            errors.append(f"invalid persisted drift metadata for event {row['event_id']}")
            break
    for row in order_rows:
        expected_factor = controller.get_factor(row["order_timestamp"])
        if (
            row["drift_enabled"] is not True
            or row["drift_scenario"] != config.drift.scenario
            or row["drift_phase"] != controller.get_phase(row["order_timestamp"])
            or abs(row["drift_factor"] - expected_factor) > 1e-8
        ):
            errors.append(f"invalid persisted drift metadata for order {row['order_id']}")
            break

    if features is not None and order_rows and event_rows:
        order_dates_by_user: dict[int, list] = defaultdict(list)
        event_dates_by_user: dict[int, list] = defaultdict(list)
        for row in order_rows:
            order_dates_by_user[row["user_id"]].append(
                row["order_timestamp"].date()
            )
        canonical_events = {}
        for row in event_rows:
            canonical_events.setdefault(row["event_id"], row)
        for row in canonical_events.values():
            event_dates_by_user[row["user_id"]].append(
                row["event_timestamp"].date()
            )
        for values in order_dates_by_user.values():
            values.sort()
        for values in event_dates_by_user.values():
            values.sort()

        for row in features.to_pylist():
            feature_date = row["feature_date"]
            window_start = feature_date - timedelta(days=89)
            order_dates = order_dates_by_user.get(row["user_id"], [])
            event_dates = event_dates_by_user.get(row["user_id"], [])
            expected_orders = bisect_right(order_dates, feature_date) - bisect_left(
                order_dates, window_start
            )
            expected_events = bisect_right(event_dates, feature_date) - bisect_left(
                event_dates, window_start
            )
            if (
                row["f_user_total_orders_90d"] != expected_orders
                or row["f_user_purchase_count_90d"] != expected_orders
                or row["f_user_interaction_count_90d"] != expected_events
            ):
                errors.append(
                    "rolling feature point-in-time mismatch for "
                    f"user={row['user_id']} date={feature_date}"
                )
                break

    return ValidationResult(
        errors=errors,
        metrics={"drift_enabled": True, "artifact_row_counts": row_counts},
    )
