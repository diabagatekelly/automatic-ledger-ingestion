"""Shared builders for WhatsApp webhook payloads used across tests.

Keeps the envelope shape in one place so slices that touch the payload
(text, image, voice) update a single fixture.
"""

from __future__ import annotations

from typing import Any


def text_message_envelope(body: str) -> dict[str, Any]:
    """A realistic WhatsApp Cloud API inbound text-message webhook envelope."""
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
