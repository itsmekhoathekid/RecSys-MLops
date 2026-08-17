from __future__ import annotations

import json

import pytest

from rag_data.contracts import GeneratedItemContent
from rag_data.orcarouter_client import (
    OrcaRouterClient,
    OrcaRouterRetryExhausted,
    OrcaRouterTerminalError,
)


def generated_payload() -> dict:
    return {
        "title": "Tai nghe demo",
        "description": "Nội dung synthetic.",
        "specifications": {"battery": "30 giờ"},
        "usage_instructions": "Bật nguồn.",
        "reviews": [
            {"content": "Tốt.", "sentiment_aspects": {"sound": "positive"}},
            {"content": "Ổn.", "sentiment_aspects": {"comfort": "neutral"}},
        ],
        "qna_pairs": [{"question": "Kết nối PC?", "answer": "Có."}],
    }


class Response:
    def __init__(
        self, status_code: int, body: dict | None = None, headers: dict | None = None
    ):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return next(self.responses)


def success_response(payload: dict | None = None) -> Response:
    return Response(
        200,
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(payload or generated_payload())},
                }
            ]
        },
    )


def client(session, sleeps=None):
    return OrcaRouterClient(
        api_key="test-key",
        session=session,
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


def test_valid_json_uses_openai_compatible_contract():
    session = Session([success_response()])
    result = client(session).generate("grounding")
    request = session.calls[0][1]
    assert result.title == "Tai nghe demo"
    response_format = request["json"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "generated_item_content"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == (
        GeneratedItemContent.model_json_schema()
    )
    assert set(response_format["json_schema"]["schema"]["properties"]) == {
        "title",
        "description",
        "specifications",
        "usage_instructions",
        "reviews",
        "qna_pairs",
    }
    assert result.finish_reason == "stop"
    assert request["json"]["stream"] is False
    assert request["headers"]["Authorization"] == "Bearer test-key"


def test_invalid_json_retries_with_repair_prompt():
    session = Session(
        [
            Response(200, {"choices": [{"message": {"content": "not json"}}]}),
            success_response(),
        ]
    )
    result = client(session).generate("grounding")
    assert result.title == "Tai nghe demo"
    messages = session.calls[1][1]["json"]["messages"]
    assert "Lỗi validation" in messages[-1]["content"]


def test_json_parser_accepts_reasoning_prefix_without_storing_it():
    content = "<think>internal reasoning</think>\n" + json.dumps(generated_payload())
    session = Session([Response(200, {"choices": [{"message": {"content": content}}]})])

    result = client(session).generate("grounding")

    assert result.title == "Tai nghe demo"


def test_429_honors_retry_after():
    sleeps = []
    session = Session([Response(429, headers={"Retry-After": "7"}), success_response()])
    assert client(session, sleeps).generate("grounding").title
    assert sleeps == [7.0]


@pytest.mark.parametrize("status", [400, 401, 403])
def test_auth_and_bad_request_are_terminal(status):
    session = Session([Response(status)])
    with pytest.raises(OrcaRouterTerminalError) as error:
        client(session).generate("grounding")
    assert error.value.attempts == 1
    assert len(session.calls) == 1


def test_5xx_retries_then_raises():
    sleeps = []
    session = Session([Response(500), Response(502), Response(503)])
    with pytest.raises(OrcaRouterRetryExhausted) as error:
        client(session, sleeps).generate("grounding")
    assert error.value.attempts == 3
    assert sleeps == [1, 2]
