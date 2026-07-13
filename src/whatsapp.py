"""Parsing helpers for the WhatsApp Cloud API webhook envelope.

Meta wraps every inbound event in the same nested shape:

    entry[].changes[].value.messages[]

Text messages carry their content at ``.text.body``. Image messages carry a
media *ID* at ``.image.id`` (the bytes are fetched separately, see
``src.media``) plus an optional ``.image.caption``. Delivery/read *status*
callbacks arrive on the same webhook but under ``value.statuses`` (no
``messages``). This module reduces any such payload to the text bodies and/or
image references to persist; everything else yields nothing so the caller can
simply ACK.

Pure and I/O-free, so it is unit-tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboundImage:
    """A reference to an inbound image message: its media ID and any caption."""

    media_id: str
    caption: str


def _dicts(value: Any) -> list[dict[str, Any]]:
    """Return only the dict elements of a value that should be a JSON array.

    Anything that isn't a list (``None``, a bare dict, a scalar) collapses to
    an empty list, so an off-spec payload is skipped rather than raising.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def extract_message_texts(payload: dict[str, Any]) -> list[str]:
    """Return the ``text.body`` of every text message in a webhook payload.

    Tolerates missing keys, status/non-text callbacks, and off-spec container
    types by yielding an empty list rather than raising.
    """
    texts: list[str] = []
    if not isinstance(payload, dict):
        return texts
    for entry in _dicts(payload.get("entry")):
        for change in _dicts(entry.get("changes")):
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for message in _dicts(value.get("messages")):
                if message.get("type") == "text":
                    text = message.get("text")
                    body = text.get("body") if isinstance(text, dict) else None
                    if body:
                        texts.append(body)
    return texts


def extract_image_messages(payload: dict[str, Any]) -> list[InboundImage]:
    """Return an ``InboundImage`` for every image message in a webhook payload.

    An image message carries its media ID at ``image.id`` and an optional
    ``image.caption``. Missing keys, non-image messages, and off-spec container
    types yield nothing rather than raising; an image without a media ID (which
    could not be fetched) is skipped.
    """
    images: list[InboundImage] = []
    if not isinstance(payload, dict):
        return images
    for entry in _dicts(payload.get("entry")):
        for change in _dicts(entry.get("changes")):
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for message in _dicts(value.get("messages")):
                if message.get("type") != "image":
                    continue
                image = message.get("image")
                if not isinstance(image, dict):
                    continue
                media_id = image.get("id")
                if not isinstance(media_id, str) or not media_id:
                    continue
                caption = image.get("caption")
                images.append(
                    InboundImage(
                        media_id=media_id,
                        caption=caption if isinstance(caption, str) else "",
                    )
                )
    return images
