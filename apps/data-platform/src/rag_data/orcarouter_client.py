"""OpenAI-compatible OrcaRouter client with strict JSON Schema generation.

Requests carry one product and never include secrets in logs or artifacts.
Terminal authentication/request errors fail immediately; rate limits, transient
server errors, and invalid schema responses retry at most the configured count.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import requests
from pydantic import ValidationError

from rag_data.contracts import GeneratedItemContent
from rag_data.prompts import SYSTEM_PROMPT, repair_prompt


class OrcaRouterError(RuntimeError):
    """Base provider error carrying attempts, HTTP status, and finish reason."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        status_code: int | None = None,
        finish_reason: str | None = None,
    ):
        super().__init__(message)
        self.attempts = attempts
        self.status_code = status_code
        self.finish_reason = finish_reason


class OrcaRouterTerminalError(OrcaRouterError):
    """A non-retryable provider or authentication response."""

    pass


class OrcaRouterRetryExhausted(OrcaRouterError):
    """A retryable provider/schema failure that exhausted its attempt budget."""

    pass


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 1.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 1.0


def _content_json(raw: str) -> Any:
    value = raw.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError as original_error:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", value):
            try:
                candidate, _ = decoder.raw_decode(value[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                return candidate
        raise original_error


class OrcaRouterClient:
    """Generate one strict :class:`GeneratedItemContent` per request."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek/deepseek-v4-pro",
        base_url: str = "https://api.orcarouter.ai/v1",
        temperature: float = 0.3,
        max_tokens: int = 1800,
        max_attempts: int = 3,
        timeout_seconds: float = 120,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key.strip():
            raise ValueError("ORCAROUTER_API_KEY must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.api_key = api_key
        self.model = model
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self._provided_session = session
        self._thread_local = threading.local()
        self.sleep = sleep

    def _session(self) -> requests.Session:
        if self._provided_session is not None:
            return self._provided_session
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            self._thread_local.session = session
        return session

    def _payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "generated_item_content",
                    "strict": True,
                    "schema": GeneratedItemContent.model_json_schema(),
                },
            },
            "messages": messages,
        }

    def generate(self, prompt: str) -> GeneratedItemContent:
        """Call OrcaRouter and return validated content or a classified error.

        The method is thread-safe through one HTTP session per worker. Invalid
        JSON/schema output gets a repair turn; 429 honors ``Retry-After`` and
        supported 5xx responses use exponential backoff.
        """

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        last_error = "unknown OrcaRouter error"
        last_finish_reason: str | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._session().post(
                    self.url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=self._payload(messages),
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = f"OrcaRouter request failed: {type(exc).__name__}"
                if attempt < self.max_attempts:
                    self.sleep(2 ** (attempt - 1))
                    continue
                raise OrcaRouterRetryExhausted(last_error, attempts=attempt) from exc

            status = response.status_code
            if status in {400, 401, 403}:
                raise OrcaRouterTerminalError(
                    f"OrcaRouter returned terminal HTTP {status}",
                    attempts=attempt,
                    status_code=status,
                )
            if status == 429:
                last_error = "OrcaRouter rate limit exhausted"
                if attempt < self.max_attempts:
                    self.sleep(
                        _retry_after_seconds(response.headers.get("Retry-After"))
                    )
                    continue
                raise OrcaRouterRetryExhausted(
                    last_error, attempts=attempt, status_code=status
                )
            if status in {500, 502, 503}:
                last_error = f"OrcaRouter transient HTTP {status}"
                if attempt < self.max_attempts:
                    self.sleep(2 ** (attempt - 1))
                    continue
                raise OrcaRouterRetryExhausted(
                    last_error, attempts=attempt, status_code=status
                )
            if status >= 400:
                raise OrcaRouterTerminalError(
                    f"OrcaRouter returned unexpected HTTP {status}",
                    attempts=attempt,
                    status_code=status,
                )

            raw_content = ""
            try:
                body = response.json()
                choice = body["choices"][0]
                last_finish_reason = choice.get("finish_reason")
                raw_content = choice["message"]["content"]
                generated = GeneratedItemContent.model_validate(
                    _content_json(raw_content)
                )
                return generated.attach_finish_reason(last_finish_reason)
            except (
                KeyError,
                IndexError,
                TypeError,
                json.JSONDecodeError,
                ValidationError,
            ) as exc:
                last_error = f"Invalid OrcaRouter JSON/schema: {type(exc).__name__}"
                if attempt < self.max_attempts:
                    if raw_content:
                        messages.append({"role": "assistant", "content": raw_content})
                    messages.append(
                        {"role": "user", "content": repair_prompt(str(exc))}
                    )
                    continue
                raise OrcaRouterRetryExhausted(
                    last_error,
                    attempts=attempt,
                    finish_reason=last_finish_reason,
                ) from exc

        raise OrcaRouterRetryExhausted(last_error, attempts=self.max_attempts)
