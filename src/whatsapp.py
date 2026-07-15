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

It also owns two adjacent pure concerns on the same webhook contract:
``verify_signature`` (the ``X-Hub-Signature-256`` HMAC that authenticates an
inbound POST, #8) and the reply helpers ``extract_reply_context`` /
``build_confirmation`` (who to reply to + what to say, #8). The network send of
that reply is the one piece of I/O and lives in ``src.messaging``.

Pure and I/O-free, so it is unit-tested directly.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from src.llm import has_usable_amount
from src.sheets import strip_review_marker


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


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str | None) -> bool:
    """Validate Meta's ``X-Hub-Signature-256`` header against the app secret.

    Meta signs each webhook POST with ``sha256=<hex>`` where ``<hex>`` is the
    HMAC-SHA256 of the *raw request body* keyed by the WhatsApp app secret. The
    body must be the exact received bytes — re-serializing the parsed JSON would
    change the digest. Comparison is constant-time to avoid a timing side channel.

    Fails **closed**: a missing/empty secret, a missing header, or a header
    without the ``sha256=`` prefix all return ``False`` so an unsigned or
    unverifiable POST is rejected rather than trusted.
    """
    if not app_secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    received = signature_header[len("sha256=") :]
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def extract_reply_context(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(sender, phone_number_id)`` for replying to the inbound message.

    The confirmation reply goes back to the message's ``from`` via *our* business
    number, whose id is on the same ``value.metadata.phone_number_id``. Returns
    the first message that carries both; ``None`` for status callbacks or
    off-spec payloads (no reply is sent, but the row is still appended).
    """
    if not isinstance(payload, dict):
        return None
    for entry in _dicts(payload.get("entry")):
        for change in _dicts(entry.get("changes")):
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            if not isinstance(metadata, dict):
                continue
            phone_number_id = metadata.get("phone_number_id")
            if not isinstance(phone_number_id, str) or not phone_number_id:
                continue
            for message in _dicts(value.get("messages")):
                sender = message.get("from")
                if isinstance(sender, str) and sender:
                    return (sender, phone_number_id)
    return None


def build_confirmation(row: list[str]) -> str:
    """Compose the short WhatsApp confirmation for an appended ledger row.

    Row columns: Date | Contract | Event | Type | Category | Amount | Notes |
    Status. A cleanly parsed row reads e.g. ``✅ Logged: Revenue 200 — Diallo
    wedding (Paid)`` (the most specific of event/contract/notes as the label). A
    fallback/raw row (no Type or Amount) instead flags that it wasn't read
    cleanly, so the owner knows to fix it by hand — silence never happens.

    The owner is asked to clarify ONLY when the row has no Amount — a row with no
    number is unusable. A row flagged NEEDS_REVIEW merely for low confidence gets
    the ordinary reply: a live smoke run showed a complete, correct row scoring
    low, so pestering her about those would train her to ignore the flag (#9).
    """
    date, contract, event, type_, category, amount, notes, status = (row + [""] * 8)[:8]
    notes = strip_review_marker(notes)
    if not type_ and not amount:
        return f"⚠️ Couldn't read that clearly — logged the raw text: {notes}"
    if not has_usable_amount(amount):
        # Parsed enough to have a Type, but no usable number landed — the mat /
        # blurry receipt / contentless-text case. Note this catches a literal "0"
        # too: the model invents one rather than leaving the field empty (live:
        # "Recu paiement" → amount=0), and "✅ Logged: Revenue 0" would read as a
        # clean success. Name the missing piece so she knows exactly what to send
        # rather than guessing what went wrong. An uncaptioned unreadable image
        # leaves nothing to quote back, so the detail is conditional rather than
        # a dangling colon.
        detail = f": {notes}" if notes else ""
        return f"⚠️ Couldn't find an amount — logged it for review{detail}"
    label = event or contract or notes
    message = f"✅ Logged: {type_} {amount}".rstrip()
    if label:
        message += f" — {label}"
    if status:
        message += f" ({status})"
    return message
