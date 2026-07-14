import pytest

from src.main import _UNREADABLE_IMAGE_NOTE, verify_webhook, webhook
from tests.factories import image_message_envelope, text_message_envelope


class FakeRequest:
    """Minimal stand-in for a Flask Request for the webhook entry point."""

    def __init__(
        self,
        method: str,
        args: dict[str, str] | None = None,
        json: object = None,
        headers: dict[str, str] | None = None,
        data: bytes = b"",
    ) -> None:
        self.method = method
        self.args = args or {}
        self._json = json
        self.headers = headers or {}
        self._data = data

    def get_json(self, silent: bool = False) -> object:
        return self._json

    def get_data(self) -> bytes:
        return self._data


@pytest.fixture(autouse=True)
def _bypass_signature_and_stub_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the POST tests to an authenticated request with a no-op reply.

    Signature verification (#8) and the outbound confirmation reply are exercised
    by their own dedicated tests; every other POST test asserts row-building, so
    it should not have to sign a body or hit the send API. Individual tests
    re-monkeypatch either hook when they are the thing under test.
    """
    monkeypatch.setattr("src.main.verify_signature", lambda *a, **k: True)
    monkeypatch.setattr("src.main.send_text_message", lambda *a, **k: None)


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


def test_webhook_post_appends_parsed_row_from_whatsapp_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.llm import ParsedNote

    rows: list[list[str]] = []
    monkeypatch.setattr("src.main.append_row", lambda row: rows.append(row))
    parsed = ParsedNote(
        date="2026-07-13",
        contract_name="Wedding Cake",
        category="Revenue",
        type="Revenue",
        amount="200",
        notes="Cash sale",
        confidence="high",
    )
    monkeypatch.setattr("src.main.parse_note", lambda text, today: parsed)

    _, status = webhook(FakeRequest("POST", json=text_message_envelope("Cash sale, $200")))  # type: ignore[arg-type]

    assert status == 200
    # Order: Date | Contract | Event | Type | Category | Amount | Notes | Status.
    # event/status are blank here — the note is built directly, bypassing coerce.
    assert rows == [
        ["2026-07-13", "Wedding Cake", "", "Revenue", "Revenue", "200", "Cash sale", ""]
    ]


def test_webhook_post_appends_event_and_status_columns_from_parsed_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A credit sale: a non-default event AND status must survive the full
    # webhook → build_row_from_note path and land in columns 2 (Event) and 7
    # (Status), guarding against either being dropped or mis-ordered.
    from src.llm import ParsedNote

    rows: list[list[str]] = []
    monkeypatch.setattr("src.main.append_row", lambda row: rows.append(row))
    parsed = ParsedNote(
        date="2026-07-13",
        contract_name="Diallo",
        event="Diallo wedding",
        category="Revenue",
        type="Revenue",
        amount="200",
        notes="Wedding deposit on credit",
        confidence="high",
        status="Owed to us",
    )
    monkeypatch.setattr("src.main.parse_note", lambda text, today: parsed)

    _, status = webhook(FakeRequest("POST", json=text_message_envelope("Diallo wedding, owes 200")))  # type: ignore[arg-type]

    assert status == 200
    assert rows == [
        [
            "2026-07-13",
            "Diallo",
            "Diallo wedding",
            "Revenue",
            "Revenue",
            "200",
            "Wedding deposit on credit",
            "Owed to us",
        ]
    ]


def test_webhook_post_falls_back_to_raw_text_row_when_parse_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[list[str]] = []
    monkeypatch.setattr("src.main.append_row", lambda row: rows.append(row))
    monkeypatch.setattr("src.main.parse_note", lambda text, today: None)

    _, status = webhook(FakeRequest("POST", json=text_message_envelope("Cash sale, $200")))  # type: ignore[arg-type]

    assert status == 200
    assert len(rows) == 1
    assert rows[0][0]  # Date column populated
    assert rows[0][6] == "Cash sale, $200"  # raw text preserved in Source/Notes


def test_webhook_post_appends_parsed_row_from_receipt_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.llm import ParsedNote

    rows: list[list[str]] = []
    monkeypatch.setattr("src.main.append_row", lambda row: rows.append(row))
    monkeypatch.setattr("src.main.download_media", lambda media_id: (b"jpeg-bytes", "image/jpeg"))
    parsed = ParsedNote(
        date="2026-07-10",
        contract_name="",
        category="Ingredients",
        type="Expense",
        amount="20000",
        notes="Meat purchase",
        confidence="high",
    )
    captured: dict[str, object] = {}

    def fake_parse_image(image_bytes: bytes, mime_type: str, today: object, caption: str) -> object:
        captured.update(image_bytes=image_bytes, mime_type=mime_type, caption=caption)
        return parsed

    monkeypatch.setattr("src.main.parse_image", fake_parse_image)

    _, status = webhook(FakeRequest("POST", json=image_message_envelope("media-1", caption="Meat")))  # type: ignore[arg-type]

    assert status == 200
    # Order: Date | Contract | Event | Type | Category | Amount | Notes | Status.
    assert rows == [["2026-07-10", "", "", "Expense", "Ingredients", "20000", "Meat purchase", ""]]
    assert captured == {"image_bytes": b"jpeg-bytes", "mime_type": "image/jpeg", "caption": "Meat"}


def test_webhook_post_falls_back_to_caption_row_when_image_parse_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[list[str]] = []
    monkeypatch.setattr("src.main.append_row", lambda row: rows.append(row))
    monkeypatch.setattr("src.main.download_media", lambda media_id: (b"bytes", "image/jpeg"))
    monkeypatch.setattr("src.main.parse_image", lambda *a, **k: None)

    _, status = webhook(
        FakeRequest("POST", json=image_message_envelope("media-1", caption="Lunch receipt"))  # type: ignore[arg-type]
    )

    assert status == 200
    assert len(rows) == 1
    assert rows[0][0]  # Date populated
    assert rows[0][6] == "Lunch receipt"  # caption preserved in Source/Notes


def test_webhook_post_appends_marker_row_when_media_download_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[list[str]] = []
    monkeypatch.setattr("src.main.append_row", lambda row: rows.append(row))

    def boom(media_id: str) -> object:
        raise RuntimeError("media endpoint down")

    monkeypatch.setattr("src.main.download_media", boom)

    _, status = webhook(FakeRequest("POST", json=image_message_envelope("media-1")))  # type: ignore[arg-type]

    assert status == 200
    assert len(rows) == 1  # a captionless, unfetchable photo is NOT silently dropped
    assert rows[0][0]  # Date populated
    assert rows[0][6] == _UNREADABLE_IMAGE_NOTE  # exact marker lands in Source/Notes


def test_webhook_post_appends_a_row_for_both_text_and_image_in_one_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.llm import ParsedNote

    rows: list[list[str]] = []
    monkeypatch.setattr("src.main.append_row", lambda row: rows.append(row))
    monkeypatch.setattr("src.main.download_media", lambda media_id: (b"jpeg-bytes", "image/jpeg"))
    text_note = ParsedNote("2026-07-13", "", "", "Revenue", "200", "Cash sale", "high")
    image_note = ParsedNote("2026-07-10", "ONEP", "Revenue", "Revenue", "555000", "Invoice", "high")
    monkeypatch.setattr("src.main.parse_note", lambda text, today: text_note)
    monkeypatch.setattr("src.main.parse_image", lambda *a, **k: image_note)

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"type": "text", "text": {"body": "Cash sale, $200"}},
                                {"type": "image", "image": {"id": "media-1", "caption": "Invoice"}},
                            ]
                        }
                    }
                ]
            }
        ]
    }
    _, status = webhook(FakeRequest("POST", json=payload))  # type: ignore[arg-type]

    assert status == 200
    assert len(rows) == 2  # one row per message, both paths run on the same webhook
    assert rows[0][6] == "Cash sale"  # text row appended first (Notes now col 7)
    assert rows[1][1] == "ONEP"  # image row appended second


def test_webhook_post_acks_status_callback_without_appending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[list[str]] = []
    monkeypatch.setattr("src.main.append_row", lambda row: rows.append(row))

    status_envelope = {"entry": [{"changes": [{"value": {"statuses": [{"status": "sent"}]}}]}]}
    _, status = webhook(FakeRequest("POST", json=status_envelope))  # type: ignore[arg-type]

    assert status == 200
    assert rows == []


def test_webhook_post_acks_empty_body_without_appending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[list[str]] = []
    monkeypatch.setattr("src.main.append_row", lambda row: rows.append(row))

    _, status = webhook(FakeRequest("POST", json=None))  # type: ignore[arg-type]

    assert status == 200
    assert rows == []


def test_webhook_rejects_other_methods() -> None:
    _, status = webhook(FakeRequest("DELETE"))  # type: ignore[arg-type]
    assert status == 405


# --- #8: HMAC signature verification (fail-closed) ---


def test_webhook_post_rejects_an_invalid_signature_without_appending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[list[str]] = []
    monkeypatch.setattr("src.main.append_row", lambda row: rows.append(row))
    monkeypatch.setattr("src.main.verify_signature", lambda *a, **k: False)

    _, status = webhook(FakeRequest("POST", json=text_message_envelope("Cash sale, $200")))  # type: ignore[arg-type]

    assert status == 403
    assert rows == []  # an unauthenticated POST must never write a row


def test_webhook_post_verifies_signature_over_the_raw_body_and_app_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
    monkeypatch.setattr("src.main.append_row", lambda row: None)
    monkeypatch.setattr("src.main.parse_note", lambda text, today: None)
    captured: dict[str, object] = {}

    def fake_verify(raw_body: bytes, header: str | None, secret: str | None) -> bool:
        captured.update(raw_body=raw_body, header=header, secret=secret)
        return True

    monkeypatch.setattr("src.main.verify_signature", fake_verify)

    webhook(
        FakeRequest(  # type: ignore[arg-type]
            "POST",
            json=text_message_envelope("x"),
            headers={"X-Hub-Signature-256": "sha256=abc"},
            data=b"raw-bytes",
        )
    )

    # HMAC must be checked against the EXACT received bytes, not the re-serialized
    # JSON, or a benign reordering would fail an otherwise-valid signature.
    assert captured == {"raw_body": b"raw-bytes", "header": "sha256=abc", "secret": "app-secret"}


# --- #8: confirmation reply to sender ---


def test_webhook_post_sends_a_confirmation_reply_after_appending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.llm import ParsedNote

    monkeypatch.setattr("src.main.append_row", lambda row: None)
    parsed = ParsedNote(
        date="2026-07-13",
        contract_name="Diallo",
        event="Diallo wedding",
        category="Revenue",
        type="Revenue",
        amount="200",
        notes="Deposit",
        confidence="high",
        status="Paid",
    )
    monkeypatch.setattr("src.main.parse_note", lambda text, today: parsed)
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "src.main.send_text_message",
        lambda to, body, phone_number_id: sent.append((to, body, phone_number_id)),
    )

    _, status = webhook(FakeRequest("POST", json=text_message_envelope("Diallo wedding, 200")))  # type: ignore[arg-type]

    assert status == 200
    assert len(sent) == 1  # exactly one reply per appended row
    to, body, phone_number_id = sent[0]
    assert to == "15551234567"  # reply goes back to the inbound sender
    assert phone_number_id == "123"
    assert "200" in body and "Diallo wedding" in body


def test_webhook_post_confirmation_failure_does_not_block_the_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[list[str]] = []
    monkeypatch.setattr("src.main.append_row", lambda row: rows.append(row))
    monkeypatch.setattr("src.main.parse_note", lambda text, today: None)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("send API down")

    monkeypatch.setattr("src.main.send_text_message", boom)

    _, status = webhook(FakeRequest("POST", json=text_message_envelope("Cash sale, $200")))  # type: ignore[arg-type]

    assert status == 200  # a failed reply is best-effort; the row still landed
    assert len(rows) == 1
