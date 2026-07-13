"""Shared builders for WhatsApp webhook payloads used across tests.

Keeps the envelope shape in one place so slices that touch the payload
(text, image, voice) update a single fixture.
"""

from __future__ import annotations

from typing import Any


def text_message_envelope(body: str) -> dict[str, Any]:
    """A realistic WhatsApp Cloud API inbound text-message webhook envelope."""
    return _envelope(
        {
            "from": "15551234567",
            "id": "wamid.ABC",
            "timestamp": "1710000000",
            "type": "text",
            "text": {"body": body},
        }
    )


def image_message_envelope(media_id: str, caption: str | None = None) -> dict[str, Any]:
    """A realistic WhatsApp Cloud API inbound image-message webhook envelope.

    Image messages carry the media ID (not the bytes) under ``image.id``; the
    bytes are fetched separately via the media endpoint. ``caption`` is optional.
    """
    image: dict[str, Any] = {
        "id": media_id,
        "mime_type": "image/jpeg",
        "sha256": "abc123",
    }
    if caption is not None:
        image["caption"] = caption
    return _envelope(
        {
            "from": "15551234567",
            "id": "wamid.IMG",
            "timestamp": "1710000000",
            "type": "image",
            "image": image,
        }
    )


def _envelope(message: dict[str, Any]) -> dict[str, Any]:
    """Wrap a single inbound message in the standard WhatsApp webhook envelope."""
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
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }
