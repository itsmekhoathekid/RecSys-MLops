"""Typed recommendation MCP downstream failures."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DownstreamError(RuntimeError):
    """Describe a sanitized failure without leaking downstream response bodies."""

    code: str
    service: str
    retryable: bool
    message: str

    def as_dict(self) -> dict[str, object]:
        """Return the stable tool error wire representation."""

        return {
            "code": self.code,
            "service": self.service,
            "retryable": self.retryable,
            "message": self.message,
        }
