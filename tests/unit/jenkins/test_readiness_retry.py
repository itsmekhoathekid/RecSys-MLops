import httpx

from jenkins.python.llm_agent_cd import driver


def test_readiness_get_retries_service_endpoint_race(monkeypatch):
    attempts = iter(
        [
            httpx.ConnectError("not propagated"),
            httpx.Response(
                200,
                request=httpx.Request("GET", "http://backend/health"),
            ),
        ]
    )

    def get(*args, **kwargs):
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(driver.httpx, "get", get)
    monkeypatch.setattr(driver.time, "sleep", lambda _: None)

    assert driver.wait_ready_get("http://backend/health", 1).status_code == 200
