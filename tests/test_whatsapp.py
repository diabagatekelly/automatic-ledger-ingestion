from src.whatsapp import extract_message_texts
from tests.factories import text_message_envelope


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
