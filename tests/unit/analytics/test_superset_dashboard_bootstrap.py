from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[3]


def load_bootstrap() -> ModuleType:
    path = ROOT / "apps/analytics/superset/bootstrap_dashboards.py"
    spec = importlib.util.spec_from_file_location("recsys_superset_bootstrap", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.ok = 200 <= status_code < 400


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls = 0

    def request(self, *_args: object, **_kwargs: object) -> FakeResponse:
        response = self.responses[self.calls]
        self.calls += 1
        return response


def test_transient_dataset_refresh_retries_until_trino_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = load_bootstrap()
    client = bootstrap.SupersetClient()
    session = FakeSession([FakeResponse(500, "Fatal error"), FakeResponse(200)])
    client.session = session
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)

    response = client.request(
        "PUT",
        "/api/v1/dataset/5/refresh",
        retry_transient=True,
    )

    assert response.status_code == 200
    assert session.calls == 2


def test_non_transient_superset_error_fails_without_retry() -> None:
    bootstrap = load_bootstrap()
    client = bootstrap.SupersetClient()
    session = FakeSession([FakeResponse(400, "invalid dataset")])
    client.session = session

    with pytest.raises(RuntimeError, match="invalid dataset"):
        client.request(
            "PUT",
            "/api/v1/dataset/5/refresh",
            retry_transient=True,
        )

    assert session.calls == 1
