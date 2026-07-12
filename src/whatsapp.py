"""Parsing helpers for the WhatsApp Cloud API webhook envelope.

Meta wraps every inbound event in the same nested shape:

    entry[].changes[].value.messages[]

Text messages carry their content at ``.text.body``. Delivery/read *status*
callbacks arrive on the same webhook but under ``value.statuses`` (no
``messages``), and non-text messages (image, audio, …) omit ``text``. This
module reduces any such payload to the list of plain-text bodies to persist;
everything else yields nothing so the caller can simply ACK.

Pure and I/O-free, so it is unit-tested directly.
"""

from __future__ import annotations

from typing import Any


def extract_message_texts(payload: dict[str, Any]) -> list[str]:
    """Return the ``text.body`` of every text message in a webhook payload.

    Tolerates missing keys and status/non-text callbacks by yielding an empty
    list rather than raising.
    """
    texts: list[str] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                if message.get("type") == "text":
                    body = message.get("text", {}).get("body")
                    if body:
                        texts.append(body)
    return texts
