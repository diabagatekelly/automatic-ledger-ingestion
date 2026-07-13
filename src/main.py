"""Cloud Function entry point for the catering-ledger 'Snap & Chat' webhook.

This is the walking skeleton (see Issues #1-#2). It currently:
  - answers the WhatsApp webhook verification handshake (GET)
  - parses inbound message webhooks (POST), appends a Sheet row per text
    message, and ACKs status/non-text callbacks without a row

Gemini parsing of the text into structured columns is added in later slices.
"""

from __future__ import annotations

import os
from datetime import date

import functions_framework
from flask import Request

from src.llm import parse_note
from src.sheets import append_row, build_row, build_row_from_note
from src.whatsapp import extract_message_texts


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
        # Meta delivers inbound messages and status callbacks as JSON. Extract
        # any text bodies and append a row each; status/non-text callbacks yield
        # none and are simply ACKed (Meta retries on any non-200).
        # Each text is parsed by Gemini into structured columns (#4); if the
        # parse fails we fall back to the raw text in Notes (#1) so nothing is
        # ever lost. TODO(#5): image/voice; TODO(#9): flag low-confidence rows.
        payload = request.get_json(silent=True) or {}
        today = date.today()
        for text in extract_message_texts(payload):
            note = parse_note(text, today)
            row = build_row_from_note(note) if note is not None else build_row(text, today)
            append_row(row)
        return ("", 200)

    return ("method not allowed", 405)
