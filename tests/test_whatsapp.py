from src.whatsapp import InboundImage, extract_image_messages, extract_message_texts
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
