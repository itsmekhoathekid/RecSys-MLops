from __future__ import annotations

from typing import Any

from features.flink.pyflink_compat import KeyedProcessFunction
from features.flink.runtime import apply_state_ttl


class MarkDuplicateEvents(KeyedProcessFunction):
    def __init__(self, args: Any) -> None:
        self.args = args

    def open(self, runtime_context):
        from pyflink.common import Types
        from pyflink.datastream.state import ValueStateDescriptor

        descriptor = apply_state_ttl(
            ValueStateDescriptor("seen_event_id", Types.BOOLEAN()),
            self.args.dedup_state_ttl_seconds,
        )
        self.seen = runtime_context.get_state(descriptor)

    def process_element(self, event: dict[str, Any], ctx):
        duplicate = bool(self.seen.value())
        if not duplicate:
            self.seen.update(True)
        marked = dict(event)
        marked["_is_duplicate"] = duplicate
        yield marked
