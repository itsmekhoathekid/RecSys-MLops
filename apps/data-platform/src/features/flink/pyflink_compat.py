"""Import-safe PyFlink base classes for local tests and runtime operators.

Production images provide PyFlink. The small fallbacks keep pure unit tests able to
import operator modules without installing the JVM/PyFlink runtime.
"""

from __future__ import annotations

try:
    from pyflink.datastream.functions import (
        AggregateFunction,
        AsyncFunction,
        FilterFunction,
        KeyedProcessFunction,
        MapFunction,
        ProcessWindowFunction,
    )
except ImportError:

    class AggregateFunction:  # pragma: no cover - import-only fallback
        pass

    class AsyncFunction:  # pragma: no cover - import-only fallback
        pass

    class FilterFunction:  # pragma: no cover - import-only fallback
        pass

    class KeyedProcessFunction:  # pragma: no cover - import-only fallback
        pass

    class MapFunction:  # pragma: no cover - import-only fallback
        pass

    class ProcessWindowFunction:  # pragma: no cover - import-only fallback
        pass


try:
    from pyflink.common.watermark_strategy import TimestampAssigner
except ImportError:
    try:
        from pyflink.datastream.functions import TimestampAssigner
    except ImportError:

        class TimestampAssigner:  # pragma: no cover - import-only fallback
            pass


try:
    from pyflink.datastream.window import Trigger
except ImportError:

    class Trigger:  # pragma: no cover - import-only fallback
        pass


__all__ = [
    "AggregateFunction",
    "AsyncFunction",
    "FilterFunction",
    "KeyedProcessFunction",
    "MapFunction",
    "ProcessWindowFunction",
    "TimestampAssigner",
    "Trigger",
]
