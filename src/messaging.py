"""WhatsApp Cloud API outbound message adapter.

The counterpart to ``src.media`` (which *downloads* inbound media): this sends a
short text reply back to the owner via the Graph API, authenticated with the
same ``WHATSAPP_ACCESS_TOKEN``:

    POST /{phone_number_id}/messages   {messaging_product, to, type, text}

Used for the #8 confirmation reply so the owner knows a row landed. Free within
the 24h service window. Thin I/O adapter, tested with a mocked ``requests`` — no
live calls in CI.
"""

from __future__ import annotations

import os

import requests

# Graph API version pinned to match the rest of the Meta integration (see
# src.media / docs/STATUS.md "Meta setup"). Bump deliberately, not implicitly.
_GRAPH_API_BASE = "https://graph.facebook.com/v21.0"
_TIMEOUT_SECONDS = 30


def _access_token() -> str:
    """Return the WhatsApp access token, failing fast with a clear message."""
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN environment variable is not set")
    return token


def send_text_message(to: str, body: str, phone_number_id: str) -> None:
    """Send a plain-text WhatsApp message from our business number to ``to``.

    Raises on a missing token or any HTTP error; the caller sends confirmations
    best-effort (a failed reply must never block or duplicate the Sheet append).
    """
    headers = {"Authorization": f"Bearer {_access_token()}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    response = requests.post(
        f"{_GRAPH_API_BASE}/{phone_number_id}/messages",
        headers=headers,
        json=payload,
        timeout=_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
