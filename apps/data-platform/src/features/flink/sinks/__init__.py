"""Async external sinks used by the realtime Flink graph."""

from __future__ import annotations

import json
import sys
from typing import Any


def emit_progress(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()
