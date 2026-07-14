"""Cloud Function entry point for the automatic-ledger-ingestion 'Snap & Chat' webhook.

This is the walking skeleton (see Issues #1-#2). It currently:
  - answers the WhatsApp webhook verification handshake (GET)
  - parses inbound message webhooks (POST), appends a Sheet row per text
    message, and ACKs status/non-text callbacks without a row

Gemini parsing of the text into structured columns is added in later slices.
"""

from __future__ import annotations

import logging
import os
from datetime import date

import functions_framework
from flask import Request

from src.llm import parse_image, parse_note
from src.media import download_media
from src.messaging import send_text_message
from src.sheets import append_row, build_row, build_row_from_note
from src.whatsapp import (
    InboundImage,
    build_confirmation,
    extract_image_messages,
    extract_message_texts,
    extract_reply_context,
    verify_signature,
)

logger = logging.getLogger(__name__)

# Fallback note for a receipt photo we received but could not read (download or
# parse failed) and that carried no caption — so the row is never silently lost.
_UNREADABLE_IMAGE_NOTE = "[receipt photo received — could not read it automatically]"


def verify_webhook(
    mode: str | None,
    token: str | None,
    challenge: str | None,
    expected_token: str | None,
) -> tuple[str, int]:
    """Validate Meta's webhook verification handshake.

    On success, Meta expects the ``challenge`` echoed back verbatim.
    Returns ``(body, status_code)``.
    """
    if mode == "subscribe" and token is not None and token == expected_token:
        return (challenge or "", 200)
    return ("forbidden", 403)


@functions_framework.http
def webhook(request: Request) -> tuple[str, int]:
    """HTTP entry point wired to the WhatsApp Cloud API."""
    if request.method == "GET":
        return verify_webhook(
            request.args.get("hub.mode"),
            request.args.get("hub.verify_token"),
            request.args.get("hub.challenge"),
            os.environ.get("WHATSAPP_VERIFY_TOKEN"),
        )

    if request.method == "POST":
        # The endpoint is --allow-unauthenticated (Meta must reach it), so we
        # authenticate each POST by its X-Hub-Signature-256 HMAC against the app
        # secret (#8). Verified over the RAW body, before parsing.
        raw_body = request.get_data()
        signature_header = request.headers.get("X-Hub-Signature-256")
        app_secret = os.environ.get("WHATSAPP_APP_SECRET")
        if not app_secret:
            # Misconfiguration, NOT an auth failure: the secret isn't mounted.
            # 500 (not 403) so it surfaces as an operational error and Meta
            # retries once it's fixed, instead of masquerading as a bad signature.
            logger.error("WHATSAPP_APP_SECRET is not set; cannot verify webhook signatures")
            return ("server misconfigured", 500)
        if not verify_signature(raw_body, signature_header, app_secret):
            # Fail-closed with no side effects. Log the reason (never the secret
            # or body) to separate a malformed/absent header from a real mismatch.
            reason = "missing signature header" if not signature_header else "signature mismatch"
            logger.warning("Rejected unauthenticated webhook POST: %s", reason)
            return ("forbidden", 403)

        # Meta delivers inbound messages and status callbacks as JSON. Extract
        # any text bodies and image references and append a row each; status /
        # unsupported callbacks yield none and are simply ACKed (Meta retries on
        # any non-200). Every message is parsed by Gemini into structured
        # columns (#4 text, #5 image); on any failure we fall back to a raw-text
        # row so nothing is ever lost. TODO(#9): flag low-confidence rows.
        payload = request.get_json(silent=True) or {}
        reply_context = extract_reply_context(payload)
        today = date.today()
        for text in extract_message_texts(payload):
            note = parse_note(text, today)
            row = build_row_from_note(note) if note is not None else build_row(text, today)
            append_row(row)
            _send_confirmation(reply_context, row)
        for image in extract_image_messages(payload):
            row = _row_for_image(image, today)
            append_row(row)
            _send_confirmation(reply_context, row)
        return ("", 200)

    return ("method not allowed", 405)


def _send_confirmation(reply_context: tuple[str, str] | None, row: list[str]) -> None:
    """Reply to the sender with a one-line summary of the row just appended (#8).

    Best-effort: the row is already saved, so a send failure (or an
    unidentifiable sender) is logged and swallowed. One reply per appended row.
    The pure summary is built OUTSIDE the try so a bug there surfaces normally;
    only the network send is guarded. The catch is broad on purpose — letting a
    send error propagate would 500 the webhook, and Meta would retry the whole
    delivery, producing a DUPLICATE row. Best-effort delivery is the right trade.
    """
    if reply_context is None:
        return
    sender, phone_number_id = reply_context
    message = build_confirmation(row)
    try:
        send_text_message(sender, message, phone_number_id)
    except Exception:
        logger.warning(
            "Confirmation reply to %s (phone_number_id=%s) failed; row was still appended",
            sender,
            phone_number_id,
            exc_info=True,
        )


def _row_for_image(image: InboundImage, today: date) -> list[str]:
    """Download, parse, and map a receipt photo to a ledger row.

    On a download or parse failure, falls back to a raw-text row carrying the
    caption (or a marker if none) so an unreadable photo is never silently
    dropped — the owner still sees a row to correct by hand.
    """
    note = None
    try:
        image_bytes, mime_type = download_media(image.media_id)
    except Exception:
        # Only the download is in the try: parse_image handles its own failures
        # internally (returns None + logs), so the message stays accurate.
        logger.warning("Receipt image download failed; falling back to raw row", exc_info=True)
    else:
        note = parse_image(image_bytes, mime_type, today, image.caption)
    if note is not None:
        return build_row_from_note(note)
    return build_row(image.caption or _UNREADABLE_IMAGE_NOTE, today)
