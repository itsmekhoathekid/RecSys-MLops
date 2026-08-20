"""Stable error contract returned by the MCP orchestration layer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DownstreamError(RuntimeError):
    """Classified downstream failure safe to expose to the agent runtime."""

    code: str
    service: str
    retryable: bool
    message: str

    def as_dict(self) -> dict[str, Any]:
        """Serialize the stable error contract returned to MCP consumers."""

        return {
            "code": self.code,
            "service": self.service,
            "retryable": self.retryable,
            "message": self.message,
        }

    def __str__(self) -> str:
        """Render the typed contract as JSON inside MCP tool errors."""

        return json.dumps(self.as_dict(), sort_keys=True)
