import hashlib
import hmac

from src.sheets import NEEDS_REVIEW
from src.whatsapp import (
    InboundImage,
    build_confirmation,
    extract_image_messages,
    extract_message_texts,
    extract_reply_context,
    verify_signature,
)
from tests.factories import image_message_envelope, text_message_envelope


def test_extract_returns_text_body_of_a_text_message() -> None:
    assert extract_message_texts(text_message_envelope("Cash sale, $200")) == ["Cash sale, $200"]


def test_extract_ignores_status_callbacks() -> None:
    status_payload = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [{"id": "wamid.ABC", "status": "delivered"}],
                        },
                    }
                ]
            }
        ]
    }
    assert extract_message_texts(status_payload) == []


def test_extract_ignores_non_text_messages() -> None:
    image_payload = text_message_envelope("ignored")
    message = image_payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message["type"] = "image"
    del message["text"]
    message["image"] = {"id": "media-1", "mime_type": "image/jpeg"}
    assert extract_message_texts(image_payload) == []


# --- extract_image_messages (pure) ---


def test_extract_images_returns_media_id_and_caption() -> None:
    payload = image_message_envelope("media-1", caption="Lunch receipt")
    assert extract_image_messages(payload) == [
        InboundImage(media_id="media-1", caption="Lunch receipt")
    ]


def test_extract_images_defaults_caption_to_empty_string() -> None:
    payload = image_message_envelope("media-1")
    assert extract_image_messages(payload) == [InboundImage(media_id="media-1", caption="")]


def test_extract_images_ignores_text_messages() -> None:
    assert extract_image_messages(text_message_envelope("Cash sale, $200")) == []


def test_extract_images_skips_image_without_media_id() -> None:
    payload = image_message_envelope("ignored")
    del payload["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["id"]
    assert extract_image_messages(payload) == []


def test_extract_images_tolerates_off_spec_payloads() -> None:
    assert extract_image_messages(None) == []  # type: ignore[arg-type]
    assert extract_image_messages({}) == []
    assert extract_image_messages({"entry": [{"changes": [{"value": None}]}]}) == []
    assert extract_image_messages({"entry": [{"changes": [{"value": {"messages": {}}}]}]}) == []
    assert (
        extract_image_messages(
            {"entry": [{"changes": [{"value": {"messages": [{"type": "image", "image": None}]}}]}]}
        )
        == []
    )


def test_extract_handles_multiple_messages_across_entries() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"type": "text", "text": {"body": "first"}},
                                {"type": "text", "text": {"body": "second"}},
                            ]
                        }
                    }
                ]
            },
            {"changes": [{"value": {"messages": [{"type": "text", "text": {"body": "third"}}]}}]},
        ]
    }
    assert extract_message_texts(payload) == ["first", "second", "third"]


def test_extract_tolerates_empty_or_malformed_payload() -> None:
    assert extract_message_texts({}) == []
    assert extract_message_texts({"entry": [{}]}) == []
    assert extract_message_texts({"entry": [{"changes": [{"value": {}}]}]}) == []


def test_extract_skips_text_message_with_empty_body() -> None:
    assert extract_message_texts(text_message_envelope("")) == []


def test_extract_tolerates_off_spec_container_types() -> None:
    # Keys present but the wrong shape (None, dict-instead-of-list, etc.)
    # must yield [] rather than raising a TypeError.
    assert extract_message_texts(None) == []  # type: ignore[arg-type]
    assert extract_message_texts({"entry": None}) == []
    assert extract_message_texts({"entry": {"changes": []}}) == []
    assert extract_message_texts({"entry": [None]}) == []
    assert extract_message_texts({"entry": [{"changes": None}]}) == []
    assert extract_message_texts({"entry": [{"changes": [{"value": None}]}]}) == []
    assert extract_message_texts({"entry": [{"changes": [{"value": {"messages": {}}}]}]}) == []
    assert (
        extract_message_texts(
            {"entry": [{"changes": [{"value": {"messages": [{"type": "text", "text": None}]}}]}]}
        )
        == []
    )


# --- verify_signature (pure, #8 HMAC auth) ---


def _sign(body: bytes, secret: str) -> str:
    """Meta's X-Hub-Signature-256 header value for a body + app secret."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_a_correct_signature() -> None:
    body = b'{"entry":[]}'
    assert verify_signature(body, _sign(body, "app-secret"), "app-secret") is True


def test_verify_signature_rejects_a_tampered_body() -> None:
    header = _sign(b'{"entry":[]}', "app-secret")
    assert verify_signature(b'{"entry":[{"evil":1}]}', header, "app-secret") is False


def test_verify_signature_rejects_wrong_secret() -> None:
    body = b'{"entry":[]}'
    assert verify_signature(body, _sign(body, "app-secret"), "other-secret") is False


def test_verify_signature_rejects_missing_header() -> None:
    assert verify_signature(b"body", None, "app-secret") is False


def test_verify_signature_rejects_header_without_sha256_prefix() -> None:
    # A bare hex digest (no "sha256=") must not be accepted.
    digest = hmac.new(b"app-secret", b"body", hashlib.sha256).hexdigest()
    assert verify_signature(b"body", digest, "app-secret") is False


def test_verify_signature_fails_closed_when_secret_unset() -> None:
    # No configured app secret → reject everything rather than trusting the body.
    body = b"body"
    assert verify_signature(body, _sign(body, "whatever"), None) is False
    assert verify_signature(body, _sign(body, "whatever"), "") is False


# --- extract_reply_context (pure, #8 confirmation reply) ---


def test_extract_reply_context_returns_sender_and_phone_number_id() -> None:
    payload = text_message_envelope("Cash sale, $200")
    assert extract_reply_context(payload) == ("15551234567", "123")


def test_extract_reply_context_reads_image_message_sender() -> None:
    payload = image_message_envelope("media-1", caption="Lunch")
    assert extract_reply_context(payload) == ("15551234567", "123")


def test_extract_reply_context_returns_none_for_status_callback() -> None:
    status_payload = {"entry": [{"changes": [{"value": {"statuses": [{"status": "sent"}]}}]}]}
    assert extract_reply_context(status_payload) is None


def test_extract_reply_context_tolerates_off_spec_payloads() -> None:
    assert extract_reply_context(None) is None  # type: ignore[arg-type]
    assert extract_reply_context({}) is None
    assert extract_reply_context({"entry": [{"changes": [{"value": None}]}]}) is None
    assert extract_reply_context({"entry": [{"changes": [{"value": {"messages": [{}]}}]}]}) is None
    # metadata present but no usable phone_number_id → no reply target
    assert (
        extract_reply_context(
            {"entry": [{"changes": [{"value": {"metadata": {}, "messages": [{"from": "1"}]}}]}]}
        )
        is None
    )


# --- build_confirmation (pure, #8 confirmation reply) ---


def test_build_confirmation_summarizes_a_parsed_row() -> None:
    # Date | Contract | Event | Type | Category | Amount | Notes | Status
    row = ["2026-07-13", "Diallo", "Diallo wedding", "Revenue", "Revenue", "200", "Deposit", "Paid"]
    message = build_confirmation(row)
    assert "Revenue" in message
    assert "200" in message
    assert "Diallo wedding" in message  # the event is the most specific label
    assert "Paid" in message


def test_build_confirmation_falls_back_to_contract_when_no_event() -> None:
    row = ["2026-07-13", "ONEP", "", "Revenue", "Revenue", "555000", "Invoice", "Owed to us"]
    message = build_confirmation(row)
    assert "ONEP" in message
    assert "Owed to us" in message


def test_build_confirmation_flags_a_raw_fallback_row() -> None:
    # A fallback row has no Type/Amount/Status — the owner must know it wasn't
    # parsed cleanly rather than getting silence or a misleading "logged".
    row = ["2026-07-13", "", "", "", "", "", "Cash sale, $200", ""]
    message = build_confirmation(row)
    assert "Cash sale, $200" in message
    assert "✅" not in message  # not a clean success


# --- the two-tier reply (Issue #9) ---
#
# Both tiers carry NEEDS_REVIEW in the Sheet; only a blank Amount earns a reply
# asking the owner to clarify. Low-confidence-WITH-an-amount gets the normal
# reply on purpose: a live smoke run produced a complete, correct row that scored
# low confidence, so asking her to re-send it would be a false positive — and a
# flag she learns to ignore is worse than no flag (see issue #9).


def test_build_confirmation_asks_for_clarification_when_amount_is_blank() -> None:
    # Parsed enough to have a Type, but no number — nothing usable landed.
    row = ["2026-07-13", "", "Wedding", "Revenue", "Revenue", "", f"{NEEDS_REVIEW} — Recu", "Paid"]
    message = build_confirmation(row)
    assert "✅" not in message
    assert NEEDS_REVIEW not in message  # internal marker, not owner-facing


def test_build_confirmation_asks_for_clarification_when_amount_is_zero() -> None:
    # Live case: "Recu paiement" → Gemini returned amount=0, not blank. Without
    # this she'd get "✅ Logged: Revenue 0 — Recu paiement (Paid)": a confident
    # success carrying an invented number.
    notes = f"{NEEDS_REVIEW} — Recu paiement"
    row = ["2026-07-15", "", "", "Revenue", "Revenue", "0", notes, "Paid"]
    message = build_confirmation(row)
    assert "✅" not in message
    assert "amount" in message.lower()
    assert "Recu paiement" in message
    assert NEEDS_REVIEW not in message


def test_build_confirmation_is_normal_for_low_confidence_with_an_amount() -> None:
    # The wedding-cake case: scored low, but the row is complete and correct.
    # She gets the ordinary confirmation; the flag is a quiet Sheet-side signal.
    row = [
        "2026-07-15",
        "",
        "Wedding Cake",
        "Revenue",
        "Revenue",
        "200",
        f"{NEEDS_REVIEW} — Cash sale for wedding cake",
        "Paid",
    ]
    message = build_confirmation(row)
    assert message.startswith("✅")
    assert "200" in message
    assert "Wedding Cake" in message
    assert NEEDS_REVIEW not in message


def test_build_confirmation_strips_the_marker_when_notes_is_the_label() -> None:
    # With no event/contract the label falls back to Notes — which carries the
    # marker. The owner must never see the internal token.
    row = ["2026-07-15", "", "", "Revenue", "Revenue", "200", f"{NEEDS_REVIEW} — Cash sale", "Paid"]
    message = build_confirmation(row)
    assert "Cash sale" in message
    assert NEEDS_REVIEW not in message


def test_build_confirmation_strips_a_bare_marker() -> None:
    # An uncaptioned image Gemini couldn't read: coerce_note leaves notes empty
    # (raw_text is ""), so the whole cell is just the marker.
    row = ["2026-07-15", "", "Wedding", "Revenue", "Revenue", "200", NEEDS_REVIEW, "Paid"]
    message = build_confirmation(row)
    assert message.startswith("✅")
    assert NEEDS_REVIEW not in message


def test_build_confirmation_has_no_dangling_colon_when_no_notes_to_quote() -> None:
    # Blank amount AND no notes text — don't send her "...for review: " with
    # nothing after it.
    row = ["2026-07-15", "", "", "Revenue", "Revenue", "", NEEDS_REVIEW, "Paid"]
    message = build_confirmation(row)
    assert NEEDS_REVIEW not in message
    assert not message.rstrip().endswith(":")
    assert "amount" in message.lower()
