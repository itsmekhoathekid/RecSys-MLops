from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from features.flink.item_features_job import ItemFeatureState
from features.flink.runtime_config import apply_state_ttl
from features.flink.time_utils import isoformat_utc, parse_event_time
from features.flink.user_aggregate_job import UserAggregateState
from features.flink.user_sequence_job import UserSequenceState


def _event_key(event: dict[str, Any]) -> tuple[datetime, str]:
    return parse_event_time(event["event_timestamp"]), str(event["event_id"])


def create_feature_pane_accumulator() -> dict[str, dict[str, Any]]:
    return {}


def add_event_to_feature_pane(
    event: dict[str, Any],
    accumulator: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not event.get("_is_duplicate"):
        accumulator[str(event["event_id"])] = event
    return accumulator


def merge_feature_pane_accumulators(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {**left, **right}


def feature_pane_result(
    accumulator: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    events = sorted(accumulator.values(), key=_event_key)
    return {"events": events, "event_count": len(events)}


def _window_iso(timestamp_ms: int) -> str:
    return isoformat_utc(datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc))


def attach_pane_metadata(
    aggregate: dict[str, Any],
    *,
    kind: str,
    entity_id: int,
    window_start_ms: int,
    window_end_ms: int,
    current_watermark_ms: int,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "entity_id": int(entity_id),
        "window_start_ms": int(window_start_ms),
        "window_end_ms": int(window_end_ms),
        "window_start": _window_iso(window_start_ms),
        "window_end": _window_iso(window_end_ms),
        "is_final": int(current_watermark_ms) >= int(window_end_ms) - 1,
        "events": aggregate["events"],
        "event_count": int(aggregate["event_count"]),
    }


def upsert_pane_revision(
    panes: dict[int, dict[str, Any]],
    pane: dict[str, Any],
    retention_seconds: int,
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    updated = dict(panes)
    updated[int(pane["window_start_ms"])] = pane
    latest_window_end_ms = max(
        int(value["window_end_ms"]) for value in updated.values()
    )
    cutoff_ms = latest_window_end_ms - max(1, int(retention_seconds)) * 1000
    updated = {
        start_ms: value
        for start_ms, value in updated.items()
        if int(value["window_end_ms"]) > cutoff_ms
    }
    by_event_id: dict[str, dict[str, Any]] = {}
    for value in updated.values():
        for event in value["events"]:
            by_event_id[str(event["event_id"])] = event
    return updated, sorted(by_event_id.values(), key=_event_key)


def _add_window_metadata(
    payload: dict[str, Any], pane: dict[str, Any]
) -> dict[str, Any]:
    return {
        **payload,
        "window_start": pane["window_start"],
        "window_end": pane["window_end"],
        "is_final": bool(pane["is_final"]),
    }


def build_user_feature_update(
    panes: dict[int, dict[str, Any]],
    pane: dict[str, Any],
    *,
    max_history_length: int,
    retention_seconds: int,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any] | None]:
    if panes.get(int(pane["window_start_ms"])) == pane:
        return dict(panes), None
    updated_panes, events = upsert_pane_revision(panes, pane, retention_seconds)
    if not events:
        return updated_panes, None

    sequence_state = UserSequenceState(
        max_history_length=max(1, int(max_history_length))
    )
    aggregate_state = UserAggregateState()
    sequence_payload = None
    aggregate_payload = None
    for event in events:
        sequence_payload = sequence_state.update(event)
        aggregate_payload = aggregate_state.update(event)
    assert sequence_payload is not None and aggregate_payload is not None
    latest_event = events[-1]
    return updated_panes, {
        "kind": "user",
        "event": latest_event,
        "sequence_payload": _add_window_metadata(sequence_payload, pane),
        "aggregate_payload": _add_window_metadata(aggregate_payload, pane),
        "window_start": pane["window_start"],
        "window_end": pane["window_end"],
        "is_final": bool(pane["is_final"]),
    }


def build_item_feature_update(
    panes: dict[int, dict[str, Any]],
    pane: dict[str, Any],
    *,
    retention_seconds: int,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any] | None]:
    if panes.get(int(pane["window_start_ms"])) == pane:
        return dict(panes), None
    updated_panes, events = upsert_pane_revision(panes, pane, retention_seconds)
    if not events:
        return updated_panes, None

    item_state = ItemFeatureState()
    item_payload = None
    for event in events:
        item_payload = item_state.update(event)
    assert item_payload is not None
    latest_event = events[-1]
    return updated_panes, {
        "kind": "item",
        "event": latest_event,
        "item_payload": _add_window_metadata(item_payload, pane),
        "window_start": pane["window_start"],
        "window_end": pane["window_end"],
        "is_final": bool(pane["is_final"]),
    }


def native_feature_pane_aggregate():
    from pyflink.datastream.functions import AggregateFunction

    class FeaturePaneAggregate(AggregateFunction):
        def create_accumulator(self):
            return create_feature_pane_accumulator()

        def add(self, event: dict[str, Any], accumulator: dict[str, dict[str, Any]]):
            return add_event_to_feature_pane(event, accumulator)

        def get_result(self, accumulator: dict[str, dict[str, Any]]):
            return feature_pane_result(accumulator)

        def merge(self, left, right):
            return merge_feature_pane_accumulators(left, right)

    return FeaturePaneAggregate()


def native_feature_pane_window_function(kind: str):
    from pyflink.datastream.functions import ProcessWindowFunction

    class FeaturePaneWindowFunction(ProcessWindowFunction):
        def process(self, key, context, aggregates):
            aggregate = next(iter(aggregates))
            window = context.window()
            yield attach_pane_metadata(
                aggregate,
                kind=kind,
                entity_id=int(key),
                window_start_ms=int(window.start),
                window_end_ms=int(window.end),
                current_watermark_ms=int(context.current_watermark()),
            )

    return FeaturePaneWindowFunction()


def early_and_event_time_trigger(interval_seconds: int, state_name: str):
    """Fire changed panes early, at watermark close, and for accepted-late revisions."""
    from pyflink.common import Types
    from pyflink.datastream.state import ValueStateDescriptor
    from pyflink.datastream.window import Trigger, TriggerResult

    class EarlyAndEventTimeTrigger(Trigger):
        def __init__(self):
            self.early_timer = ValueStateDescriptor(state_name, Types.LONG())
            self.dirty = ValueStateDescriptor(f"{state_name}-dirty", Types.BOOLEAN())
            self.interval_millis = max(1, int(interval_seconds)) * 1000

        def on_element(self, element, timestamp, window, ctx):
            if window.max_timestamp() <= ctx.get_current_watermark():
                ctx.get_partitioned_state(self.dirty).clear()
                return TriggerResult.FIRE
            ctx.register_event_time_timer(window.max_timestamp())
            ctx.get_partitioned_state(self.dirty).update(True)
            timer_state = ctx.get_partitioned_state(self.early_timer)
            if timer_state.value() is None:
                next_timer = (
                    int(ctx.get_current_processing_time()) + self.interval_millis
                )
                timer_state.update(next_timer)
                ctx.register_processing_time_timer(next_timer)
            return TriggerResult.CONTINUE

        def on_processing_time(self, time, window, ctx):
            timer_state = ctx.get_partitioned_state(self.early_timer)
            if timer_state.value() != time:
                return TriggerResult.CONTINUE
            timer_state.clear()
            dirty_state = ctx.get_partitioned_state(self.dirty)
            if ctx.get_current_watermark() >= window.max_timestamp():
                dirty_state.clear()
                return TriggerResult.CONTINUE
            if not dirty_state.value():
                return TriggerResult.CONTINUE
            dirty_state.clear()
            return TriggerResult.FIRE

        def on_event_time(self, time, window, ctx):
            if time != window.max_timestamp():
                return TriggerResult.CONTINUE
            timer_state = ctx.get_partitioned_state(self.early_timer)
            early_timer = timer_state.value()
            if early_timer is not None:
                ctx.delete_processing_time_timer(int(early_timer))
                timer_state.clear()
            ctx.get_partitioned_state(self.dirty).clear()
            return TriggerResult.FIRE

        def can_merge(self):
            return False

        def on_merge(self, window, ctx):
            return None

        def clear(self, window, ctx):
            ctx.delete_event_time_timer(window.max_timestamp())
            timer_state = ctx.get_partitioned_state(self.early_timer)
            early_timer = timer_state.value()
            if early_timer is not None:
                ctx.delete_processing_time_timer(int(early_timer))
                timer_state.clear()
            ctx.get_partitioned_state(self.dirty).clear()

    return EarlyAndEventTimeTrigger()


def _read_panes(map_state: Any) -> dict[int, dict[str, Any]]:
    return {int(key): value for key, value in map_state.items()}


def _replace_panes(map_state: Any, panes: dict[int, dict[str, Any]]) -> None:
    map_state.clear()
    for key, value in panes.items():
        map_state.put(int(key), value)


def native_user_rolling_feature_process(args: Any):
    from pyflink.common import Types
    from pyflink.datastream.functions import KeyedProcessFunction
    from pyflink.datastream.state import MapStateDescriptor

    class UserRollingFeatureProcess(KeyedProcessFunction):
        def open(self, runtime_context):
            descriptor = apply_state_ttl(
                MapStateDescriptor(
                    "user-feature-pane-revisions",
                    Types.LONG(),
                    Types.PICKLED_BYTE_ARRAY(),
                ),
                args.state_ttl_seconds,
            )
            self.panes = runtime_context.get_map_state(descriptor)

        def process_element(self, pane, ctx):
            panes, update = build_user_feature_update(
                _read_panes(self.panes),
                pane,
                max_history_length=args.max_history_length,
                retention_seconds=args.state_ttl_seconds,
            )
            _replace_panes(self.panes, panes)
            if update is not None:
                yield update

    return UserRollingFeatureProcess()


def native_item_rolling_feature_process(args: Any):
    from pyflink.common import Types
    from pyflink.datastream.functions import KeyedProcessFunction
    from pyflink.datastream.state import MapStateDescriptor

    class ItemRollingFeatureProcess(KeyedProcessFunction):
        def open(self, runtime_context):
            descriptor = apply_state_ttl(
                MapStateDescriptor(
                    "item-feature-pane-revisions",
                    Types.LONG(),
                    Types.PICKLED_BYTE_ARRAY(),
                ),
                args.state_ttl_seconds,
            )
            self.panes = runtime_context.get_map_state(descriptor)

        def process_element(self, pane, ctx):
            panes, update = build_item_feature_update(
                _read_panes(self.panes),
                pane,
                retention_seconds=args.state_ttl_seconds,
            )
            _replace_panes(self.panes, panes)
            if update is not None:
                yield update

    return ItemRollingFeatureProcess()
