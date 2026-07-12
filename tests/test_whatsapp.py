from src.whatsapp import extract_message_texts


def _text_message_payload(body: str) -> dict:
    """A minimal WhatsApp Cloud API inbound text-message webhook envelope."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "123"},
                            "messages": [
                                {
                                    "from": "15551234567",
                                    "id": "wamid.ABC",
                                    "timestamp": "1710000000",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_extract_returns_text_body_of_a_text_message() -> None:
    assert extract_message_texts(_text_message_payload("Cash sale, $200")) == ["Cash sale, $200"]


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
    image_payload = _text_message_payload("ignored")
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
    assert extract_message_texts(_text_message_payload("")) == []
