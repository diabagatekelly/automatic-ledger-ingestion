import pytest

from src.main import verify_webhook, webhook


class FakeRequest:
    """Minimal stand-in for a Flask Request for the webhook entry point."""

    def __init__(self, method: str, args: dict[str, str] | None = None) -> None:
        self.method = method
        self.args = args or {}


# --- verify_webhook (pure) ---


def test_verify_webhook_success_echoes_challenge() -> None:
    body, status = verify_webhook("subscribe", "s3cret", "12345", "s3cret")
    assert status == 200
    assert body == "12345"


def test_verify_webhook_rejects_bad_token() -> None:
    _, status = verify_webhook("subscribe", "wrong", "12345", "s3cret")
    assert status == 403


def test_verify_webhook_rejects_missing_token() -> None:
    _, status = verify_webhook("subscribe", None, "12345", "s3cret")
    assert status == 403


def test_verify_webhook_rejects_wrong_mode() -> None:
    _, status = verify_webhook("unsubscribe", "s3cret", "12345", "s3cret")
    assert status == 403


# --- webhook (HTTP entry point) ---


def test_webhook_get_completes_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "s3cret")
    req = FakeRequest(
        "GET",
        {"hub.mode": "subscribe", "hub.verify_token": "s3cret", "hub.challenge": "99"},
    )
    body, status = webhook(req)  # type: ignore[arg-type]
    assert status == 200
    assert body == "99"


def test_webhook_post_acks() -> None:
    _, status = webhook(FakeRequest("POST"))  # type: ignore[arg-type]
    assert status == 200


def test_webhook_rejects_other_methods() -> None:
    _, status = webhook(FakeRequest("DELETE"))  # type: ignore[arg-type]
    assert status == 405
