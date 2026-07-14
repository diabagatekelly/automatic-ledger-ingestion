from typing import Any

import pytest

from src.messaging import send_text_message


class FakeHTTPResponse:
    """Minimal stand-in for a ``requests.Response``."""

    def __init__(self, *, json_body: Any = None) -> None:
        self._json = json_body
        self.raised = False

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        self.raised = True


class FakeRequests:
    """Records POST calls and returns a canned response."""

    def __init__(self, response: FakeHTTPResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeHTTPResponse:
        self.calls.append({"url": url, **kwargs})
        return self._response


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-token")


def test_send_text_message_posts_to_the_number_messages_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRequests(FakeHTTPResponse(json_body={"messages": [{"id": "wamid.OUT"}]}))
    monkeypatch.setattr("src.messaging.requests", fake)

    send_text_message("15551234567", "✅ Logged: Revenue 200", "1208667628996715")

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"].endswith("/1208667628996715/messages")
    assert call["headers"]["Authorization"] == "Bearer test-token"
    assert call["json"] == {
        "messaging_product": "whatsapp",
        "to": "15551234567",
        "type": "text",
        "text": {"body": "✅ Logged: Revenue 200"},
    }


def test_send_text_message_raises_for_status(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom(FakeHTTPResponse):
        def raise_for_status(self) -> None:
            raise RuntimeError("Graph 400")

    monkeypatch.setattr("src.messaging.requests", FakeRequests(Boom()))
    with pytest.raises(RuntimeError, match="Graph 400"):
        send_text_message("15551234567", "hi", "123")


def test_send_text_message_raises_without_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="WHATSAPP_ACCESS_TOKEN"):
        send_text_message("15551234567", "hi", "123")
